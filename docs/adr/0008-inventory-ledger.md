# 0008: Inventory is an append-only ledger with locked balances

**Status:** Accepted, 2026-09-26 (Stage 3)

## Decision
- Every stock change is a `StockMovement` (signed quantity, reason, source, actor, correlation id). PostgreSQL rejects UPDATE/DELETE on the table.
- `StockBalance` holds on_hand / reserved / damaged per product and location. Check constraints forbid negatives and `reserved > on_hand`.
- Only `apps.inventory.services` writes stock. Each operation locks the affected balances with `SELECT … FOR UPDATE` in a stable order, re-checks the rule under the lock, and writes the movement in the same transaction. Optional idempotency keys make retries safe.
- Reservations are separate rows. Consuming one at dispatch writes a `SALE` movement.
- A nightly task reconciles balances against the ledger and active reservations, and logs any mismatch at ERROR. It never auto-corrects.
- Human overrides are `StockAdjustment` (a note is required) and `StockCount` (completion needs its own permission; agents cannot complete counts).

## Consequences
"Why is it 43?" is always answerable from the ledger. Corrections are new movements, never edits. Concurrency is covered by real multi-thread tests and a property-based invariant test.
