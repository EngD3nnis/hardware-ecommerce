"""Tool registry and dispatcher: the only way an agent touches Dewmix (ADR 0004).

dispatch() enforces, in order, for every call:
1. the automation kill switch;
2. the tool is on this agent's allow-list;
3. the tool's tier is within the agent's maximum tier. HIGH-tier tools never
   execute: they create an ApprovalRequest for a person. There are no
   CRITICAL tools at all;
4. the input validates against the tool's Pydantic model;
5. the handler runs (it calls domain services, which re-check business rules),
   as the agent's Actor;
6. the call is recorded (ToolCall) with its input, output and status.
Errors come back to the model as structured tool errors; they never crash the run
and never leave half-written business data (services are transactional).
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum

from pydantic import BaseModel, ValidationError

from apps.automation.services import automations_enabled, request_approval
from apps.core.actors import Actor
from apps.core.exceptions import DomainError

from ..models import AgentRun, ToolCall, ToolCallStatus
from ..providers.base import ToolSpec

logger = logging.getLogger(__name__)


class Tier(IntEnum):
    LOW = 1  # read, search, summarise, draft text
    MEDIUM = 2  # create drafts, internal tasks, alerts, approved-template messages
    HIGH = 3  # change prices/stock, cancel orders, approve spending: approval required
    CRITICAL = 4  # money transfers, destructive operations: no tools exist, by design


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    tier: Tier
    input_model: type[BaseModel]
    handler: Callable | None = None  # (ctx, input) -> dict. None for HIGH tools.
    approval_action: str = ""  # HIGH tools: automation action code
    approval_summary: Callable | None = None  # HIGH tools: (input) -> str

    def spec(self) -> ToolSpec:
        schema = self.input_model.model_json_schema()
        schema.pop("title", None)
        return ToolSpec(name=self.name, description=self.description, input_schema=schema)


@dataclass
class ToolContext:
    agent: str
    run: AgentRun
    actor: Actor
    allowed_tools: frozenset[str]
    max_tier: Tier


_TOOLS: dict[str, Tool] = {}


def register(tool: Tool) -> Tool:
    if tool.tier >= Tier.CRITICAL:
        raise ValueError("CRITICAL actions must not be exposed as tools.")
    if tool.tier == Tier.HIGH and not tool.approval_action:
        raise ValueError(f"HIGH tool {tool.name} must route to an approval action.")
    if tool.tier < Tier.HIGH and tool.handler is None:
        raise ValueError(f"Tool {tool.name} needs a handler.")
    _TOOLS[tool.name] = tool
    return tool


def get_tool(name: str) -> Tool | None:
    return _TOOLS.get(name)


def all_tools() -> dict[str, Tool]:
    return dict(_TOOLS)


def specs_for(names) -> list[ToolSpec]:
    return [_TOOLS[n].spec() for n in sorted(names) if n in _TOOLS]


def dispatch(name: str, raw_input: dict, ctx: ToolContext) -> tuple[dict, bool]:
    """Run one tool call. Returns (result, is_error) for the model."""
    started = time.monotonic()
    tool = _TOOLS.get(name)
    record = ToolCall(run=ctx.run, tool=name, tier=tool.tier.name if tool else "?", input=raw_input or {})

    def finish(status, output, error=""):
        record.status, record.output, record.error = status, output, error[:2000]
        record.duration_ms = int((time.monotonic() - started) * 1000)
        record.save()
        return output, status in (ToolCallStatus.ERROR, ToolCallStatus.DENIED)

    if not automations_enabled():
        return finish(
            ToolCallStatus.DENIED,
            {"error": "Automations are switched off. Stop and hand over to staff."},
            "kill switch",
        )
    if tool is None or name not in ctx.allowed_tools:
        return finish(ToolCallStatus.DENIED, {"error": f"Tool {name!r} is not available to you."}, "not allowed")
    if tool.tier > ctx.max_tier:
        return finish(ToolCallStatus.DENIED, {"error": f"{name} needs higher permission than you have."}, "tier")
    try:
        data = tool.input_model.model_validate(raw_input or {})
    except ValidationError as exc:
        errors = [{"field": ".".join(map(str, e["loc"])), "problem": e["msg"]} for e in exc.errors()]
        return finish(ToolCallStatus.ERROR, {"error": "Invalid input.", "details": errors}, str(errors))

    if tool.tier == Tier.HIGH:
        values = data.model_dump(mode="json")
        reason = values.get("reason", "")
        request = request_approval(
            tool.approval_action,
            summary=tool.approval_summary(data) if tool.approval_summary else f"{ctx.agent}: {name}",
            values=values,
            requested_by=ctx.actor,
            reason=reason,
        )
        return finish(
            ToolCallStatus.PENDING_APPROVAL,
            {
                "status": "pending_approval",
                "approval_request": str(request.pk),
                "message": "A person must approve this. Tell the customer/staff it is waiting for approval; do not "
                "say it is done.",
            },
        )
    try:
        output = tool.handler(ctx, data)
    except DomainError as exc:
        return finish(ToolCallStatus.ERROR, {"error": exc.message, "details": exc.details}, exc.message)
    except Exception:  # noqa: BLE001 - a tool bug must not crash the agent; logged with traceback
        logger.exception("Tool raised an unexpected error", extra={"tool": name, "run": str(ctx.run.pk)})
        return finish(
            ToolCallStatus.ERROR,
            {"error": "Internal error in this tool; staff have been notified."},
            "unexpected exception",
        )
    return finish(ToolCallStatus.OK, output)
