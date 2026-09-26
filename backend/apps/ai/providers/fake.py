"""Deterministic provider for tests and local development: replays a script of completions."""

from .base import Completion, ProviderError, ToolCallRequest


class FakeProvider:
    name = "fake"

    def __init__(self, script: list[dict] | None = None):
        # Each step: {"text": "...", "tool_calls": [{"name":..., "input":{...}}]} or {"error": "..."}
        self.script = list(script or [{"text": "OK"}])
        self.requests: list[dict] = []

    def complete(self, *, model, system, messages, tools, max_tokens, effort, timeout) -> Completion:
        self.requests.append({"system": system, "messages": list(messages), "tools": [t.name for t in tools]})
        step = self.script.pop(0) if self.script else {"text": "Done."}
        if "error" in step:
            raise ProviderError(step["error"])
        calls = [
            ToolCallRequest(id=f"call_{len(self.requests)}_{i}", name=c["name"], input=c.get("input", {}))
            for i, c in enumerate(step.get("tool_calls", []))
        ]
        return Completion(
            text=step.get("text", ""),
            tool_calls=calls,
            stop_reason=step.get("stop_reason", "tool_use" if calls else "end_turn"),
            input_tokens=step.get("input_tokens", 100),
            output_tokens=step.get("output_tokens", 20),
            model=model,
            provider_content=None,
        )
