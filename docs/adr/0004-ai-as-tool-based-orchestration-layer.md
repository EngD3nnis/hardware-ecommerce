# 0004: AI is an optional orchestration layer that acts only through audited tools

**Status:** Accepted, 2026-09-26 (implementation: Stages 8–9)

## Context

LLMs are useful for understanding customer requests, drafting quotes and descriptions, cleaning catalogue data and summarising operations. They also hallucinate, fail, change behaviour between versions and cost money per call. If a model could write to the database, run SQL or invent a price, one bad output could corrupt orders or promise stock that doesn't exist. The initial scaffold configured three provider SDKs with no code and no abstraction, which invites provider calls being scattered through business logic.

## Decision

- AI never owns business truth. Prices, stock, orders, payments and permissions are read from and written to Django/PostgreSQL only.
- Agents act **only through explicit tools**. Each tool validates input (Pydantic), checks the agent's permission tier and allow-list, calls the same **domain service** a human would use (with the agent recorded as the actor), and returns structured output. No tool exposes SQL, shell or filesystem access.
- Permission tiers are enforced in code, not in prompts. LOW and MEDIUM actions (read, draft, alerts) run directly and are audited. HIGH actions (prices, stock adjustments, cancellations, purchase approval, refunds) create an approval request that a human must approve. CRITICAL actions have no tool at all.
- Provider SDKs are imported only in `apps/ai/providers/`, behind one `AIProvider` interface. Which provider and model each agent uses is configuration.
- Every agent can be disabled individually, and a global kill switch (database flag plus an environment override) disables all automation without affecting the core application.
- Deterministic questions (e.g. "what is the stock of SKU X?") are answered by queries, never by a model.

## Consequences

- An AI failure can only produce a failed automation run and an alert, never a corrupted business record. Humans can always continue manually.
- More upfront code (tool registry, approvals, usage tracking) than calling an SDK directly. That is accepted as the cost of safety and auditability.
- Changing LLM provider does not touch agents or business code.
