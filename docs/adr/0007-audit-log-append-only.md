# 0007: Audit log is written by services and enforced append-only in the database

**Status:** Accepted, 2026-09-26 (Stage 2; originally planned for Stage 4)

## Context

Catalogue, price and (later) stock, order and payment changes must answer "who changed what, when, from what, and why", including when the actor is an import, an integration or an AI agent. Audit rows that an admin, a script or a bug can edit or delete are not evidence.

## Decision

- `apps.audit.AuditEvent` records the actor (type, label, user id, agent run id), the action, the object, before/after values, a reason, the correlation id, and the client IP and user agent.
- Services call `audit.record()` explicitly, inside the same transaction as the change. There are no model signals: the audit is visible in the code that makes the change, and it commits or rolls back with it.
- Actor and object references are plain values, not foreign keys, so deleting a user or product never cascades into its history.
- Migration `audit/0002` installs a PostgreSQL trigger that rejects UPDATE and DELETE on the table for every role. The admin is read-only.
- The audit app moved forward from Stage 4 to Stage 2, because catalogue and price changes needed auditing from the start.

## Consequences

- Correcting a mistaken audit row is impossible by design. Record a new event instead.
- TRUNCATE is not blocked, because Django's test framework needs it. The production database role for the app should not be granted TRUNCATE on this table.
- The table grows without bound. Archival (e.g. yearly partitions or an export) is a future operations task.
