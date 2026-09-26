from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

import pytest
from django.contrib.auth.models import Permission
from django.urls import reverse

from apps.ai import runner
from apps.ai.models import AgentConfig, AgentRun, ModelCall, RunStatus, ToolCall, ToolCallStatus
from apps.ai.providers import override_provider
from apps.ai.providers.anthropic_provider import AnthropicProvider
from apps.ai.providers.base import Message, ToolSpec
from apps.ai.providers.fake import FakeProvider
from apps.ai.tools.registry import Tier, Tool, ToolContext, dispatch, register
from apps.automation import services as automation
from apps.automation.models import ApprovalRequest, ApprovalStatus, Incident
from apps.catalog.tests.factories import ProductFactory
from apps.core.actors import Actor
from apps.core.exceptions import PermissionDenied
from apps.inventory import services as inventory
from apps.pricing import services as pricing
from apps.sales.models import Quotation

pytestmark = pytest.mark.django_db


@pytest.fixture
def fake():
    provider = FakeProvider()
    override_provider("fake", provider)
    yield provider
    override_provider("fake", None)


def enable(name, **extra):
    AgentConfig.objects.update_or_create(name=name, defaults={"enabled": True, "provider": "fake", **extra})


@pytest.fixture
def tap(system_actor):
    product = ProductFactory(sku="TAP-9", name="Chrome Basin Tap", price_on_request=False)
    pricing.set_price(product, "1200", system_actor)
    inventory.receive(product, 3, system_actor)
    return product


def ctx_for(agent="sales", tools=None, tier=Tier.MEDIUM):
    run = AgentRun.objects.create(agent=agent, trigger="test", task="t")
    return ToolContext(
        agent=agent,
        run=run,
        actor=Actor.agent(agent, run_id=str(run.pk)),
        allowed_tools=frozenset(tools or []),
        max_tier=tier,
    )


# --- Tool dispatch security -----------------------------------------------------------------------


def test_tool_not_on_allow_list_is_denied(tap):
    output, is_error = dispatch("get_price", {"sku": "TAP-9"}, ctx_for(tools=["search_products"]))
    assert is_error and "not available" in output["error"]
    assert ToolCall.objects.get().status == ToolCallStatus.DENIED


def test_tier_above_agent_maximum_is_denied(tap):
    ctx = ctx_for(tools=["request_price_change"], tier=Tier.MEDIUM)
    output, is_error = dispatch("request_price_change", {"sku": "TAP-9", "amount": "1", "reason": "cheaper"}, ctx)
    assert is_error and ApprovalRequest.objects.count() == 0


def test_high_tier_creates_approval_and_changes_nothing(tap):
    ctx = ctx_for(agent="inventory", tools=["request_price_change"], tier=Tier.HIGH)
    output, is_error = dispatch(
        "request_price_change", {"sku": "TAP-9", "amount": "999.50", "reason": "match competitor"}, ctx
    )
    assert not is_error and output["status"] == "pending_approval"
    request = ApprovalRequest.objects.get()
    assert request.status == ApprovalStatus.PENDING_APPROVAL and request.proposed_values["amount"] == "999.50"
    assert request.agent_run_id == str(ctx.run.pk)
    assert pricing.current_price(tap).amount == Decimal("1200")  # untouched


def test_critical_tools_cannot_be_registered():
    from pydantic import BaseModel

    with pytest.raises(ValueError):
        register(Tool("transfer_money", "x", Tier.CRITICAL, BaseModel, handler=lambda c, d: {}))


def test_invalid_input_is_reported_to_model(tap):
    output, is_error = dispatch("get_price", {"sku": ""}, ctx_for(tools=["get_price"]))
    assert is_error and output["error"] == "Invalid input."


def test_domain_errors_become_tool_errors():
    output, is_error = dispatch("get_product", {"sku": "NOPE"}, ctx_for(tools=["get_product"]))
    assert is_error and "No product" in output["error"]


def test_kill_switch_denies_tools(tap, system_actor):
    automation.set_all_disabled(True, system_actor, reason="test")
    output, is_error = dispatch("get_price", {"sku": "TAP-9"}, ctx_for(tools=["get_price"]))
    assert is_error and "switched off" in output["error"]


def test_hard_disable_setting(settings):
    settings.AUTOMATION_HARD_DISABLE = True
    assert automation.automations_enabled() is False


def test_price_tool_never_invents_prices(system_actor):
    ProductFactory(sku="CEM-1")  # no price
    output, _ = dispatch("get_price", {"sku": "CEM-1"}, ctx_for(tools=["get_price"]))
    assert output["price_on_request"] is True and output["amount"] is None


def test_availability_is_a_band_not_a_count(tap):
    output, _ = dispatch("check_availability", {"sku": "TAP-9"}, ctx_for(tools=["check_availability"]))
    assert output["availability"] == "in_stock" and "on_hand" not in output


# --- Runner -------------------------------------------------------------------------------------------


def test_sales_agent_drafts_quote_with_system_prices(fake, tap):
    enable("sales")
    fake.script = [
        {"tool_calls": [{"name": "search_products", "input": {"query": "basin tap"}}]},
        {
            "tool_calls": [
                {
                    "name": "create_quote_draft",
                    "input": {
                        "customer_phone": "0711222333",
                        "customer_name": "Mwangi",
                        "lines": [{"sku": "TAP-9", "quantity": 2}],
                    },
                }
            ]
        },
        {"text": "I've prepared quotation for you; staff will confirm shortly."},
    ]
    run = runner.run_agent("sales", "Customer 0711222333 wants 2 basin taps", trigger="test")
    assert run.status == RunStatus.SUCCEEDED and run.steps == 3
    quote = Quotation.objects.get()
    assert quote.created_by_agent and quote.status == "DRAFT"
    assert quote.lines.get().unit_price == Decimal("1200.00")
    assert ModelCall.objects.filter(run=run).count() == 3
    assert fake.requests[0]["tools"] == sorted(runner.AGENTS["sales"].tools)
    # the tool result was fed back to the model
    assert fake.requests[1]["messages"][-1].tool_results[0]["content"].startswith('{"results"')


def test_disabled_agent_does_not_run(fake):
    run = runner.run_agent("sales", "hi", trigger="test")
    assert run.status == RunStatus.BLOCKED and fake.requests == []


def test_kill_switch_blocks_runs(fake, system_actor):
    enable("sales")
    automation.set_all_disabled(True, system_actor)
    assert runner.run_agent("sales", "hi", trigger="test").status == RunStatus.BLOCKED


def test_provider_failure_fails_run_safely_and_raises_incident(fake):
    enable("operations")
    fake.script = [{"error": "Anthropic is down"}]
    run = runner.run_agent("operations", "daily check", trigger="test")
    assert run.status == RunStatus.FAILED and "Anthropic is down" in run.error
    assert Incident.objects.filter(kind="agent_failure").exists()


def test_step_limit(fake):
    enable("operations", max_steps=2)
    fake.script = [{"tool_calls": [{"name": "get_operations_snapshot"}]}] * 5
    run = runner.run_agent("operations", "loop forever", trigger="test")
    assert run.status == RunStatus.FAILED and "2 steps" in run.error


def test_refusal_escalates(fake):
    enable("sales")
    fake.script = [{"text": "", "stop_reason": "refusal"}]
    run = runner.run_agent("sales", "something", trigger="test")
    assert run.status == RunStatus.ESCALATED
    from apps.automation.models import InternalTask

    assert InternalTask.objects.exists()


def test_daily_budget_disables_agent(fake, settings):
    settings.AI_MODEL_PRICES = {"claude-opus-5": (1_000_000, 0)}  # $1 per input token
    enable("operations", daily_budget_usd=Decimal("50"))
    fake.script = [{"text": "done", "input_tokens": 100}]
    runner.run_agent("operations", "a", trigger="test")
    blocked = runner.run_agent("operations", "b", trigger="test")
    assert blocked.status == RunStatus.BLOCKED
    assert AgentConfig.objects.get(name="operations").enabled is False


# --- Approvals ------------------------------------------------------------------------------------------


def test_approval_executes_as_the_person(tap, staff_user):
    request = automation.request_approval(
        "pricing.set_price",
        summary="price",
        requested_by=Actor.agent("inventory"),
        values={"sku": "TAP-9", "amount": "1300", "reason": "supplier cost up"},
    )
    with pytest.raises(PermissionDenied):
        automation.approve(request, staff_user)  # no permission
    staff_user.user_permissions.add(
        *Permission.objects.filter(codename__in=["decide_approvalrequest", "change_product"])
    )
    user = type(staff_user).objects.get(pk=staff_user.pk)
    done = automation.approve(request, user, final_values={"sku": "TAP-9", "amount": "1250"})
    assert done.status == ApprovalStatus.EXECUTED
    assert pricing.current_price(tap).amount == Decimal("1250")
    from apps.audit.models import AuditEvent

    assert AuditEvent.objects.filter(action="pricing.price.set", actor_user_id=str(user.pk)).exists()


def test_failed_execution_is_recorded(staff_user):
    staff_user.user_permissions.add(Permission.objects.get(codename="decide_approvalrequest"))
    user = type(staff_user).objects.get(pk=staff_user.pk)
    request = automation.request_approval(
        "sales.cancel_order",
        summary="x",
        requested_by=Actor.agent("ops"),
        values={"order_number": "SO-NOPE", "reason": "dup"},
    )
    done = automation.approve(request, user)
    assert done.status == ApprovalStatus.FAILED and done.error


# --- Anthropic provider request shape --------------------------------------------------------------------


def test_anthropic_provider_builds_request_and_parses_response(settings):
    settings.AI_REFUSAL_FALLBACKS = True
    block_text = SimpleNamespace(type="text", text="Checking.", to_dict=lambda: {"type": "text", "text": "Checking."})
    block_tool = SimpleNamespace(
        type="tool_use",
        id="tu_1",
        name="get_price",
        input={"sku": "TAP-9"},
        to_dict=lambda: {"type": "tool_use", "id": "tu_1", "name": "get_price", "input": {"sku": "TAP-9"}},
    )
    response = SimpleNamespace(
        content=[block_text, block_tool],
        stop_reason="tool_use",
        model="claude-opus-5",
        usage=SimpleNamespace(input_tokens=50, output_tokens=10),
    )
    client = mock.MagicMock()
    client.with_options.return_value.beta.messages.create.return_value = response
    provider = AnthropicProvider(client=client)
    history = [Message(role="user", text="price of TAP-9?")]
    result = provider.complete(
        model="claude-opus-5",
        system="sys",
        messages=history,
        tools=[ToolSpec("get_price", "d", {"type": "object"})],
        max_tokens=4000,
        effort="medium",
        timeout=30,
    )
    kwargs = client.with_options.return_value.beta.messages.create.call_args.kwargs
    assert kwargs["model"] == "claude-opus-5" and kwargs["output_config"] == {"effort": "medium"}
    assert kwargs["betas"] == ["server-side-fallback-2026-07-01"] and kwargs["fallbacks"] == "default"
    assert "thinking" not in kwargs  # adaptive by default on current models
    assert result.tool_calls[0].name == "get_price" and result.text == "Checking."

    history += [
        Message(role="assistant", provider_content=result.provider_content),
        Message(role="user", tool_results=[{"id": "tu_1", "content": "{}", "is_error": False}]),
    ]
    api = AnthropicProvider._to_api(history)
    assert api[1]["content"][1]["type"] == "tool_use"  # replayed unchanged
    assert api[2]["content"][0] == {"type": "tool_result", "tool_use_id": "tu_1", "content": "{}", "is_error": False}


def test_unimplemented_providers_fail_cleanly(fake):
    enable("operations", provider="openai")
    run = runner.run_agent("operations", "x", trigger="test")
    assert run.status == RunStatus.FAILED and "not implemented" in run.error


# --- Control centre -----------------------------------------------------------------------------------------


def test_control_centre_and_kill_button(client, superuser, staff_user):
    client.force_login(superuser)
    assert client.get(reverse("ops-control-centre")).status_code == 200
    client.post(reverse("ops-toggle-all"), {"disable": "1", "reason": "incident"})
    assert automation.automations_enabled() is False
    client.post(reverse("ops-toggle-agent", args=["sales"]))
    assert AgentConfig.objects.get(name="sales").enabled is True
    client.force_login(staff_user)  # staff without permission can view but not switch
    client.post(reverse("ops-toggle-all"), {"disable": "0"})
    assert automation.automations_enabled() is False
    client.logout()
    assert client.get(reverse("ops-control-centre")).status_code == 302  # login required
