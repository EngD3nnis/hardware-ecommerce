# Architecture Decision Records

Short records of significant decisions: the context, what was decided, and the consequences. They explain *why* the code looks the way it does.

- Add a new ADR for any decision a future developer might otherwise reverse without knowing the reason.
- Never rewrite an accepted ADR. To change a decision, add a new ADR that supersedes it and update the old one's status line.
- Copy the format of an existing ADR: Status, Context, Decision, Consequences.

| # | Decision | Status |
|---|---|---|
| [0001](0001-modular-monolith.md) | Modular monolith, no microservices | Accepted |
| [0002](0002-postgresql-system-of-record.md) | PostgreSQL is the system of record; tests run on PostgreSQL | Accepted |
| [0003](0003-celery-and-redis-for-background-work.md) | Celery + Redis for background work; business transactions are not in tasks | Accepted |
| [0004](0004-ai-as-tool-based-orchestration-layer.md) | AI is an optional orchestration layer that acts only through audited tools | Accepted |
| [0005](0005-fail-closed-configuration.md) | Configuration fails closed in production | Accepted |
| [0006](0006-catalogue-model.md) | Catalogue: Product is the SKU, typed attributes, content-addressed images | Accepted |
| [0007](0007-audit-log-append-only.md) | Audit log written by services, append-only in the database | Accepted |
