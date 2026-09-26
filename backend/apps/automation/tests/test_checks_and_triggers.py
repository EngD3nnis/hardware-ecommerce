import hashlib
import hmac
import json
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.ai.models import AgentConfig, AgentRun, RunStatus
from apps.ai.providers import override_provider
from apps.ai.providers.fake import FakeProvider
from apps.ai.tasks import handle_inbound_message, run_scheduled_agent
from apps.automation import checks
from apps.automation.models import Incident
from apps.catalog.tests.factories import ProductFactory
from apps.customers import services as customers
from apps.inventory import services as inventory
from apps.inventory.models import StockBalance
from apps.notifications.models import InboundMessage, OutboundMessage
from apps.pricing import services as pricing
from apps.sales import services as sales
from apps.sales.models import Order

pytestmark = pytest.mark.django_db


@pytest.fixture
def fake():
    provider = FakeProvider()
    override_provider("fake", provider)
    yield provider
    override_provider("fake", None)


def test_stuck_order_check_is_deduplicated(system_actor):
    product = ProductFactory(price_on_request=False)
    pricing.set_price(product, "10", system_actor)
    customer = customers.create_customer(system_actor, name="A", phone="0700111222")
    order = sales.confirm_order(
        sales.create_order(system_actor, customer=customer, lines=[{"product": product, "quantity": 1}]), system_actor
    )
    Order.objects.filter(pk=order.pk).update(confirmed_at=timezone.now() - timedelta(days=3))
    assert checks.stuck_orders() == 1
    checks.stuck_orders()
    incident = Incident.objects.get(kind="stuck_order")
    assert incident.occurrences == 2 and order.number in incident.title


def test_inventory_mismatch_raises_critical_incident(system_actor):
    product = ProductFactory()
    inventory.receive(product, 5, system_actor)
    assert checks.inventory_reconciliation() == 0
    StockBalance.objects.filter(product=product).update(on_hand=4)
    assert checks.inventory_reconciliation() == 1
    assert Incident.objects.get(kind="inventory_mismatch").severity == "CRITICAL"


def test_run_all_with_nothing_wrong():
    assert checks.run_all() == {
        "stuck_orders": 0,
        "failed_messages": 0,
        "payment_problems": 0,
        "unanswered_whatsapp": 0,
        "expired_approvals": 0,
    }


def test_scheduled_agents_skip_when_disabled(fake):
    assert run_scheduled_agent("operations") == "skipped"
    assert not AgentRun.objects.exists() and fake.requests == []


def test_scheduled_agent_runs_when_enabled(fake):
    AgentConfig.objects.create(name="operations", enabled=True, provider="fake")
    fake.script = [{"tool_calls": [{"name": "get_operations_snapshot"}]}, {"text": "All quiet today."}]
    assert run_scheduled_agent("operations") == RunStatus.SUCCEEDED
    assert AgentRun.objects.get().output == "All quiet today."


def test_inbound_whatsapp_triggers_sales_agent_once(client, settings, fake, django_capture_on_commit_callbacks):
    settings.WHATSAPP_APP_SECRET = "s3cret"
    AgentConfig.objects.create(name="sales", enabled=True, provider="fake")
    fake.script = [
        {"tool_calls": [{"name": "reply_to_customer", "input": {"inbound_message_id": 0, "text": "placeholder"}}]},
        {"text": "Replied."},
    ]
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "id": "wamid.X1",
                                    "from": "254711000222",
                                    "type": "text",
                                    "text": {"body": "Habari, mna kufuli?"},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()

    def deliver():
        with django_capture_on_commit_callbacks(execute=True):
            client.post("/webhooks/whatsapp/", body, content_type="application/json", HTTP_X_HUB_SIGNATURE_256=sig)

    inbound_id = None
    original = handle_inbound_message.run

    def patched(i):
        nonlocal inbound_id
        inbound_id = i
        fake.script[0]["tool_calls"][0]["input"]["inbound_message_id"] = i
        return original(i)

    handle_inbound_message.run = patched
    try:
        deliver()
        deliver()  # Meta redelivers: no second run
    finally:
        handle_inbound_message.run = original
    run = AgentRun.objects.get(trigger=f"whatsapp:inbound:{inbound_id}")
    assert run.status == RunStatus.SUCCEEDED
    assert "Habari, mna kufuli?" in run.task
    reply = OutboundMessage.objects.get(template="agent_reply")
    assert reply.to == "254711000222" and reply.body == "placeholder"
    assert InboundMessage.objects.get().handled is True


def test_inbound_ignored_when_sales_agent_off(fake):
    message = InboundMessage.objects.create(provider_message_id="w1", from_phone="254700000001", body="hi", payload={})
    assert handle_inbound_message(message.pk) == "skipped"
