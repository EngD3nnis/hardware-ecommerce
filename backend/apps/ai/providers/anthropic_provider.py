"""Anthropic (Claude) provider, using the official `anthropic` SDK.

- Manual tool loop (our runner owns it) so every tool call passes our permission checks.
- Adaptive thinking is on by default on current models; depth is controlled with `effort`.
- Server-side refusal fallbacks are enabled by default (settings.AI_REFUSAL_FALLBACKS):
  if the model declines, the API retries on a fallback model inside the same call.
"""

import anthropic
from django.conf import settings

from .base import Completion, Message, ProviderError, ToolCallRequest

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, client: anthropic.Anthropic | None = None):
        self._client = client

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            if not settings.ANTHROPIC_API_KEY:
                raise ProviderError("ANTHROPIC_API_KEY is not configured.")
            self._client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, max_retries=2)
        return self._client

    @staticmethod
    def _to_api(messages: list[Message]) -> list[dict]:
        api = []
        for m in messages:
            if m.role == "assistant":
                # Replay the provider's own content unchanged (thinking blocks, tool_use ids…).
                api.append({"role": "assistant", "content": m.provider_content or m.text})
                continue
            content = [
                {
                    "type": "tool_result",
                    "tool_use_id": r["id"],
                    "content": r["content"],
                    "is_error": r.get("is_error", False),
                }
                for r in m.tool_results
            ]
            if m.text:
                content.append({"type": "text", "text": m.text})
            api.append({"role": "user", "content": content})
        return api

    def complete(self, *, model, system, messages, tools, max_tokens, effort, timeout) -> Completion:
        request = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": self._to_api(messages),
            "tools": [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools],
            "output_config": {"effort": effort},
        }
        try:
            client = self.client.with_options(timeout=timeout)
            if settings.AI_REFUSAL_FALLBACKS:
                response = client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **request)
            else:
                response = client.messages.create(**request)
        except anthropic.RateLimitError as exc:
            raise ProviderError("Rate limited by Anthropic.") from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError("Could not reach Anthropic.") from exc

        text = "".join(b.text for b in response.content if b.type == "text")
        calls = [
            ToolCallRequest(id=b.id, name=b.name, input=dict(b.input)) for b in response.content if b.type == "tool_use"
        ]
        return Completion(
            text=text,
            tool_calls=calls,
            stop_reason=response.stop_reason or "",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            model=response.model,
            provider_content=[b.to_dict() for b in response.content],
        )
