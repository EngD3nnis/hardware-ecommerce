# 0001: Modular monolith, no microservices

**Status:** Accepted, 2026-09-26

## Context

Dewmix is a single family hardware business with a very small technical team. Its critical operations (quote → order → stock reservation → payment → fulfilment) must be consistent with each other: a reserved item must not be sold twice, and a payment must attach to exactly one order. Splitting these into services would turn local database transactions into distributed ones, and would add network failure modes, deployment units and monitoring that nobody is available to operate.

## Decision

One Django project, one PostgreSQL database, one deployable. Code is organised into Django apps by **business domain** (catalog, inventory, quotations, orders, payments, …). Each app follows the same shape: `models.py`, `services.py` for writes, `selectors.py` for reads, `api/`, `admin.py`, thin `tasks.py`, and `tests/`.

Boundaries are kept by convention and review: other apps call a domain's **services**, not its models' internals. That keeps a later extraction possible without paying for it now.

## Consequences

- Cross-domain operations use ordinary `transaction.atomic()` blocks and row locks.
- One codebase to read, one process type to deploy (plus Celery workers from the same code).
- Kubernetes, Kafka, service meshes and per-service databases are explicitly out of scope. Revisit only when a component needs independent scaling **and** there is a team to own it (target-state §9).
- Discipline is needed to stop apps reaching into each other's tables. Code review and service-level tests enforce it.
