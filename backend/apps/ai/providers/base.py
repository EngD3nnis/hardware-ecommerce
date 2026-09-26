"""Provider-neutral types. Agents and tools use only these; SDKs appear only in provider modules."""

from dataclasses import dataclass, field
from typing import Any, Protocol


class ProviderError(Exception):
    """The provider call failed (network, auth, rate limit, refusal…). The run fails safely."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict


@dataclass(frozen=True)
class ToolCallRequest:
    id: str
    name: str
    input: dict


@dataclass
class Message:
    """A conversation turn.

    role: "user" | "assistant". A user turn has `text` and/or `tool_results`
    ([{"id", "content", "is_error"}]). An assistant turn keeps the provider's
    raw content in `provider_content` so it can be replayed exactly (e.g.
    thinking blocks must be sent back unchanged).
    """

    role: str
    text: str = ""
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    tool_results: list[dict] = field(default_factory=list)
    provider_content: Any = None


@dataclass
class Completion:
    text: str
    tool_calls: list[ToolCallRequest]
    stop_reason: str  # end_turn | tool_use | max_tokens | refusal | ...
    input_tokens: int
    output_tokens: int
    model: str
    provider_content: Any = None


class AIProvider(Protocol):
    name: str

    def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        max_tokens: int,
        effort: str,
        timeout: float,
    ) -> Completion: ...
