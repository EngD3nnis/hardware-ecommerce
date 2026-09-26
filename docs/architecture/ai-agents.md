# AI agents

AI is an optional orchestration layer over the Django services ([ADR 0004](../adr/0004-ai-as-tool-based-orchestration-layer.md), [ADR 0011](../adr/0011-ai-runtime.md)). **Every agent ships disabled.** With all agents off, or the kill switch on, the business runs exactly as before through the admin.

## Pieces

| Piece | Where | What it does |
|---|---|---|
| Providers | `apps/ai/providers/` | `AIProvider` interface. `anthropic` (Claude, via the official SDK) and `fake` (a scripted provider for tests) are implemented. `openai`/`gemini` are placeholders that fail cleanly until implemented. Only this package imports an SDK. |
| Tools | `apps/ai/tools/` | Typed (Pydantic) adapters over services and selectors. `registry.dispatch()` checks the kill switch, the allow-list, the tier and the input, runs the handler as the agent's Actor, and records a `ToolCall`. |
| Agents | `apps/ai/agents.py` | Purpose, instructions, allowed tools and maximum tier (code). |
| Config | `AgentConfig` (control centre / admin) | Enabled, provider, model (default `claude-opus-5`), effort (default `medium`), max steps, tokens per call, timeout, daily USD budget. |
| Runner | `apps/ai/runner.py` | The model↔tool loop. It is bounded by steps, time and budget, and records an `AgentRun` and a `ModelCall` per request (tokens, latency, cost). |
| Approvals | `apps/automation` | HIGH-tier tools create an `ApprovalRequest`. A person with `decide_approvalrequest` approves (optionally editing the values), and the registered action runs **as that person** through the normal services. |
| Kill switch | `/ops/` button, or env `AUTOMATION_HARD_DISABLE=1` | Stops every run and tool call within about 10 seconds (the cache TTL). |
| Control centre | `/ops/` | Agent status and toggles, today's runs, tokens and cost, recent runs and failures, pending approvals, incidents, tasks. |

## Permission tiers

| Tier | Examples | Behaviour |
|---|---|---|
| LOW | search, product details, price, availability band, reports | Executes |
| MEDIUM | draft quotation, draft purchase order, reply within WhatsApp's 24h window, alert, task, catalogue *proposal* | Executes (drafts and records only), audited as the agent |
| HIGH | `request_price_change`, `request_stock_adjustment`, `request_order_cancellation` | **Never executes.** Creates an approval request |
| CRITICAL | money transfers, deletions | No tool exists. `register()` refuses CRITICAL tools |

The services add their own agent guards on top: agents cannot set quote prices, record payments or refunds, cancel orders, approve/send/receive purchase orders, complete stock counts, move goods, or approve catalogue proposals.

## The agents

| Agent | Max tier | Tools | Trigger |
|---|---|---|---|
| sales | MEDIUM | search, product, availability band, price, customer, draft quote, reply, escalate | each new inbound WhatsApp message (once per message, with recent conversation as context) |
| inventory | HIGH (approval only) | low/dead stock, velocity, supplier options, draft PO, alert, task, request stock adjustment | daily 06:00 |
| catalogue | MEDIUM | catalogue issues, product, search, propose change, task | Mondays 07:00 |
| operations | MEDIUM | operations snapshot, low stock, alert, task, velocity | daily 18:30 (summary) |

Times are Nairobi time (Celery beat). A disabled agent's trigger returns immediately and costs nothing.

## Deterministic checks (no AI)

`apps/automation/checks.py` runs every 30 minutes, and the ledger reconciliation runs nightly, whether or not any agent is on. They raise deduplicated incidents for stuck orders, failed customer messages, failed payment callbacks (critical), M-Pesa requests pending over an hour, WhatsApp messages unanswered for 30+ minutes, and inventory ledger mismatches (critical). They also expire old approval requests. The operations agent only *summarises and prioritises* these facts.

## Failure behaviour

A provider error, timeout, step limit, crash or exceeded budget marks the run FAILED or BLOCKED and raises a deduplicated incident. No business data is touched, because every tool call commits through its service on its own. A refusal hands the task to staff. Anthropic requests enable server-side refusal fallbacks by default (`AI_REFUSAL_FALLBACKS`).

## Turning an agent on

1. Set `ANTHROPIC_API_KEY`.
2. In `/ops/`, enable one agent and set a small daily budget.
3. Watch its runs (Admin → Agent runs shows every model call and tool call) and its approvals.
4. Tune `effort` or the model per agent.
