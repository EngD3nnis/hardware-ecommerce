"""Provider registry. Choose per agent via AgentConfig.provider."""

from .base import AIProvider, ProviderError


class NotImplementedProvider:
    """Placeholder for providers listed in the brief but not yet implemented and verified.

    Implement against that SDK's documentation (and add tests) before enabling;
    until then selecting it fails the run cleanly instead of guessing an API.
    """

    def __init__(self, name: str):
        self.name = name

    def complete(self, **kwargs):
        raise ProviderError(f"The {self.name} provider is not implemented yet; use 'anthropic'.")


_OVERRIDES: dict[str, AIProvider] = {}


def get_provider(name: str) -> AIProvider:
    if name in _OVERRIDES:
        return _OVERRIDES[name]
    if name == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider()
    if name == "fake":
        from .fake import FakeProvider

        return FakeProvider()
    if name in ("openai", "gemini"):
        return NotImplementedProvider(name)
    raise ProviderError(f"Unknown AI provider {name!r}.")


def override_provider(name: str, provider: AIProvider | None) -> None:
    """Tests: inject a provider instance (None removes the override)."""
    if provider is None:
        _OVERRIDES.pop(name, None)
    else:
        _OVERRIDES[name] = provider


__all__ = ["AIProvider", "ProviderError", "get_provider", "override_provider"]
