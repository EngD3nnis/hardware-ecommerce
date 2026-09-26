# 0002: PostgreSQL is the system of record; tests run on PostgreSQL

**Status:** Accepted, 2026-09-26

## Context

Products, prices, stock, customers, quotes, orders and payments must have exactly one authoritative home. WhatsApp chats, the static site's embedded catalogue, search indexes and AI model outputs are all *copies* or *channels* that can be stale or wrong. The initial scaffold silently fell back to SQLite when `DATABASE_URL` was missing. SQLite has no real row-level locking (`select_for_update` is a no-op) and weaker constraint behaviour, so concurrency bugs in stock reservation would pass tests and fail in production.

## Decision

- PostgreSQL (16) is the only system of record. If any other component disagrees with it, PostgreSQL wins.
- Invariants are enforced in the database where possible (unique and check constraints, append-only triggers), not only in Python.
- Development and **all tests** use PostgreSQL: `docker compose up -d` locally, a service container in CI. There is no SQLite fallback. Production refuses to start without `DATABASE_URL` (ADR 0005).
- Full-text and fuzzy search use PostgreSQL features (`tsvector`, `pg_trgm`) before any external search engine (target-state §3.11).

## Consequences

- Running tests requires Docker (or a local Postgres). The README documents it.
- Tests can meaningfully exercise locking, constraints and concurrent reservations.
- PostgreSQL-specific features may be used freely; database portability is not a goal.
