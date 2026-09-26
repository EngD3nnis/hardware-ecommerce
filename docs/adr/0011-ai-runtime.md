# 0011: AI runtime: our own tool loop, provider interface, approvals and kill switch

**Status:** Accepted, 2026-09-26 (Stage 8)

## Decision
- We run the model↔tool loop ourselves (`apps/ai/runner.py`) instead of an SDK tool runner. That way every call passes through `registry.dispatch()`, where tier, allow-list, kill switch, validation and recording are enforced in one place we control and test.
- Agents speak a provider-neutral `Message`/`Completion` format. The Anthropic provider replays assistant content exactly, including thinking blocks, and enables server-side refusal fallbacks. OpenAI and Gemini are deliberately left as explicit "not implemented" providers rather than unverified code.
- HIGH-risk actions go to `ApprovalRequest`s executed by registered actions as the approving person. CRITICAL actions have no tools.
- Per-agent `AgentConfig` (off by default) plus a global kill switch (DB flag, cached for 10 seconds, plus an environment hard-off). Daily USD budgets auto-disable an agent.
- Every model call and tool call is persisted (`ModelCall`, `ToolCall`), so an agent can be audited and debugged after the fact.

## Consequences
Adding a capability means adding a typed tool over a service. The model never gets new powers without code review. Costs are visible per agent per day.
