import hashlib
import hmac
import json
from unittest import mock

import pytest

from apps.catalog.tests.factories import ProductFactory
from apps.core.actors import Actor
from apps.customers import services as customers
from apps.inventory import services as inventory
from apps.notifications import adapters, services
from apps.notifications.models import InboundMessage, MessageStatus, OutboundMessage
from apps.pricing import services as pricing
from apps.sales import services as sales
from apps.sales.models import OrderStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff(staff_user):
    return Actor.for_user(staff_user)


@pytest.fixture
def customer(staff):
    return customers.create_customer(staff, name="Achieng", phone="0733000111", whatsapp_opt_in=True)


def confirmed_order(staff, customer):
    product = ProductFactory(price_on_request=False)
    pricing.set_price(product, "250", staff)
    inventory.receive(product, 5, staff)
    order = sales.create_order(staff, customer=customer, lines=[{"product": product, "quantity": 2}])
    return sales.confirm_order(order, staff)


def test_order_confirmation_is_queued_and_sent_after_commit(staff, customer, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        order = confirmed_order(staff, customer)
    message = OutboundMessage.objects.get(template="order_confirmed")
    assert message.to == "254733000111" and order.number in message.body and "KES 500.00" in message.body
    assert message.status == MessageStatus.SENT and message.backend == "console"


def test_no_whatsapp_without_opt_in(staff):
    silent = customers.create_customer(staff, name="No consent", phone="0733999000")
    confirmed_order(staff, silent)
    assert not OutboundMessage.objects.exists()


def test_same_event_is_not_messaged_twice(staff, customer):
    order = confirmed_order(staff, customer)
    services.notify("order_confirmed", order, customer=customer, actor=staff)
    assert OutboundMessage.objects.filter(template="order_confirmed").count() == 1


def test_messaging_bug_never_breaks_the_sale(staff, customer):
    with mock.patch("apps.notifications.services.render", side_effect=RuntimeError("template bug")):
        order = confirmed_order(staff, customer)
    order.refresh_from_db()
    assert order.status == OrderStatus.CONFIRMED
    assert not OutboundMessage.objects.exists()


def test_transient_failures_retry_then_give_up(staff, customer, settings):
    message = services.queue(customer=customer, channel="WHATSAPP", template="order_confirmed", body="hi", actor=staff)
    with mock.patch.object(adapters.ConsoleAdapter, "send", side_effect=adapters.TransientSendError("timeout")):
        for _ in range(services.MAX_ATTEMPTS - 1):
            with pytest.raises(adapters.TransientSendError):
                services.send(str(message.pk))
        final = services.send(str(message.pk))
    assert final.status == MessageStatus.FAILED and "Gave up" in final.last_error
    with mock.patch("apps.notifications.tasks.send_message.delay") as delay:
        services.retry(final)
    delay.assert_called_once()
    final.refresh_from_db()
    assert final.status == MessageStatus.QUEUED


def test_permanent_failure_and_disabled_channel(staff, customer, settings):
    settings.NOTIFICATION_BACKENDS = {"WHATSAPP": "disabled"}
    message = services.queue(customer=customer, channel="WHATSAPP", template="x", body="hi", actor=staff)
    assert services.send(str(message.pk)).status == MessageStatus.FAILED


def test_whatsapp_cloud_adapter(staff, customer, settings):
    settings.NOTIFICATION_BACKENDS = {"WHATSAPP": "whatsapp_cloud"}
    settings.WHATSAPP_PHONE_NUMBER_ID, settings.WHATSAPP_ACCESS_TOKEN = "123", "token"
    message = services.queue(customer=customer, channel="WHATSAPP", template="order_confirmed", body="hi", actor=staff)
    ok = mock.Mock(status_code=200, json=lambda: {"messages": [{"id": "wamid.1"}]})
    with mock.patch("apps.notifications.adapters.requests.post", return_value=ok) as post:
        sent = services.send(str(message.pk))
    assert sent.provider_message_id == "wamid.1"
    assert post.call_args.kwargs["json"]["to"] == "254733000111"
    bad = services.queue(customer=customer, channel="WHATSAPP", template="t2", body="hi", actor=staff)
    with mock.patch("apps.notifications.adapters.requests.post", return_value=mock.Mock(status_code=400, text="bad")):
        assert services.send(str(bad.pk)).status == MessageStatus.FAILED


def _signed(client, payload, secret="app-secret"):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/webhooks/whatsapp/", body, content_type="application/json", HTTP_X_HUB_SIGNATURE_256=sig)


def test_whatsapp_webhook_verification_and_signature(client, settings, customer):
    settings.WHATSAPP_VERIFY_TOKEN, settings.WHATSAPP_APP_SECRET = "verify-me", "app-secret"
    ok = client.get(
        "/webhooks/whatsapp/", {"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"}
    )
    assert ok.status_code == 200 and ok.content == b"42"
    assert client.get("/webhooks/whatsapp/", {"hub.mode": "subscribe", "hub.verify_token": "no"}).status_code == 403

    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "id": "wamid.in1",
                                    "from": "254733000111",
                                    "type": "text",
                                    "text": {"body": "Do you have PPR pipes?"},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
    assert _signed(client, payload, secret="wrong").status_code == 403
    assert _signed(client, payload).status_code == 200
    assert _signed(client, payload).status_code == 200  # redelivery
    inbound = InboundMessage.objects.get()
    assert inbound.customer == customer and inbound.body == "Do you have PPR pipes?"


def test_status_updates_from_webhook(client, settings, staff, customer):
    settings.WHATSAPP_APP_SECRET = "app-secret"
    message = services.queue(customer=customer, channel="WHATSAPP", template="t", body="hi", actor=staff)
    OutboundMessage.objects.filter(pk=message.pk).update(provider_message_id="wamid.out", status="SENT")
    _signed(client, {"entry": [{"changes": [{"value": {"statuses": [{"id": "wamid.out", "status": "read"}]}}]}]})
    message.refresh_from_db()
    assert message.status == MessageStatus.READ


def test_click_to_chat_link():
    link = services.whatsapp_click_to_chat("0733 000 111", "Quote Q-2026-00001")
    assert link == "https://wa.me/254733000111?text=Quote%20Q-2026-00001"
