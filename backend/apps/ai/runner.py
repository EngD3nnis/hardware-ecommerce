"""Runs one agent task: the model ↔ tools loop, bounded by steps, time and budget.

An AI failure is only ever an automation failure: the run is marked FAILED,
an incident is raised, and business data is untouched (tools commit through
services individually; nothing is half-applied). Staff carry on manually.
"""

import json
import logging
import time
from decimal import Decimal

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from apps.automation import services as automation
from apps.automation.models import Severity
from apps.core.actors import Actor
from apps.core.request_context import get_correlation_id

from . import tools as _tools  # noqa: F401 - registers tools
from .agents import AGENTS
from .models import AgentConfig, AgentRun, ModelCall, RunStatus
from .providers import ProviderError, get_provider
from .providers.base import Message
from .tools.registry import ToolContext, dispatch, specs_for

logger = logging.getLogger(__name__)


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    input_price, output_price = settings.AI_MODEL_PRICES.get(model, (0, 0))
    return (
        Decimal(input_tokens) * Decimal(str(input_price)) + Decimal(output_tokens) * Decimal(str(output_price))
    ) / Decimal(1_000_000)


def spent_today(agent: str) -> Decimal:
    start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return AgentRun.objects.filter(agent=agent, created_at__gte=start).aggregate(t=Sum("estimated_cost_usd"))[
        "t"
    ] or Decimal(0)


def _finish(run: AgentRun, status: str, *, output: str = "", error: str = "") -> AgentRun:
    run.status, run.output, run.error, run.finished_at = status, output, error[:4000], timezone.now()
    run.save()
    if status == RunStatus.FAILED:
        automation.raise_incident(
            kind="agent_failure",
            title=f"Agent '{run.agent}' failed: {error[:120]}",
            source=run.agent,
            fingerprint=f"agent-failure:{run.agent}",
            details={"run": str(run.pk), "error": error[:1000]},
        )
    return run


def run_agent(name: str, task: str, *, trigger: str) -> AgentRun:
    definition = AGENTS[name]
    config, _ = AgentConfig.objects.get_or_create(name=name)
    run = AgentRun.objects.create(
        agent=name, trigger=trigger[:60], task=task, correlation_id=get_correlation_id() or ""
    )

    if not automation.automations_enabled():
        return _finish(run, RunStatus.BLOCKED, error="Automations are switched off.")
    if not config.enabled:
        return _finish(run, RunStatus.BLOCKED, error="This agent is disabled.")
    if spent_today(name) >= config.daily_budget_usd:
        AgentConfig.objects.filter(pk=name).update(enabled=False)
        automation.raise_incident(
            kind="agent_budget",
            title=f"Agent '{name}' hit its daily budget and was disabled",
            source=name,
            fingerprint=f"agent-budget:{name}",
            severity=Severity.WARNING,
        )
        return _finish(run, RunStatus.BLOCKED, error="Daily budget reached; agent disabled.")

    actor = Actor.agent(name, run_id=str(run.pk))
    ctx = ToolContext(agent=name, run=run, actor=actor, allowed_tools=definition.tools, max_tier=definition.max_tier)
    provider = get_provider(config.provider)
    tool_specs = specs_for(definition.tools)
    messages = [Message(role="user", text=task)]
    deadline = time.monotonic() + config.timeout_seconds
    escalated = False

    try:
        for _step in range(config.max_steps):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _finish(run, RunStatus.FAILED, error="Timed out.")
            started = time.monotonic()
            try:
                completion = provider.complete(
                    model=config.model,
                    system=definition.system_prompt,
                    messages=messages,
                    tools=tool_specs,
                    max_tokens=config.max_output_tokens,
                    effort=config.effort,
                    timeout=remaining,
                )
            except ProviderError as exc:
                ModelCall.objects.create(
                    run=run,
                    provider=provider.name,
                    model=config.model,
                    ok=False,
                    error=str(exc),
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
                return _finish(run, RunStatus.FAILED, error=f"AI provider error: {exc}")

            cost = estimate_cost(config.model, completion.input_tokens, completion.output_tokens)
            ModelCall.objects.create(
                run=run,
                provider=provider.name,
                model=completion.model,
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
                estimated_cost_usd=cost,
                stop_reason=completion.stop_reason,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
            run.steps += 1
            run.input_tokens += completion.input_tokens
            run.output_tokens += completion.output_tokens
            run.estimated_cost_usd += cost
            run.save(update_fields=["steps", "input_tokens", "output_tokens", "estimated_cost_usd", "updated_at"])

            if completion.stop_reason == "refusal":
                automation.create_task(
                    title=f"{name} agent declined a request; please handle it",
                    description=task[:2000],
                    created_by=actor,
                )
                return _finish(run, RunStatus.ESCALATED, output=completion.text, error="Model declined the request.")
            if completion.stop_reason == "max_tokens":
                return _finish(run, RunStatus.FAILED, error="Response hit the output token limit.")

            messages.append(
                Message(
                    role="assistant",
                    text=completion.text,
                    tool_calls=completion.tool_calls,
                    provider_content=completion.provider_content,
                )
            )
            if not completion.tool_calls:
                return _finish(run, RunStatus.ESCALATED if escalated else RunStatus.SUCCEEDED, output=completion.text)

            results = []
            for call in completion.tool_calls:
                output, is_error = dispatch(call.name, call.input, ctx)
                escalated = escalated or call.name == "escalate_to_human"
                results.append({"id": call.id, "content": json.dumps(output, default=str), "is_error": is_error})
            messages.append(Message(role="user", tool_results=results))
        return _finish(run, RunStatus.FAILED, error=f"Stopped after {config.max_steps} steps without finishing.")
    except Exception as exc:  # noqa: BLE001 - an agent bug must never propagate into business flows
        logger.exception("Agent run crashed", extra={"agent": name, "run": str(run.pk)})
        return _finish(run, RunStatus.FAILED, error=f"Unexpected error: {type(exc).__name__}")
