# 0003: Celery + Redis for background work; business transactions are not in tasks

**Status:** Accepted, 2026-09-26

## Context

Dewmix needs background work: notifications, catalogue/image imports, search indexing, scheduled inventory checks, reports and AI runs. These must retry on failure and must not block web requests. At the same time, a background queue is the wrong place for the *authoritative* part of a business operation. Tasks can be delayed, retried, duplicated or lost if Redis or a worker fails.

## Decision

- Celery with Redis as broker; the app lives in `backend/config/celery.py`.
- Defaults (`config/settings/base.py`): `acks_late` + `reject_on_worker_lost` (work is re-queued if a worker dies), `prefetch_multiplier=1`, soft/hard time limits of 5/6 minutes, one-day result expiry.
- Because tasks may run more than once, **every task must be idempotent**. Tasks take ids, re-read current state and do nothing if the work is already done.
- The transactional part of a business operation (creating the order, reserving stock, recording the payment) runs **synchronously in a service inside a database transaction**. Follow-up work is enqueued with `transaction.on_commit(...)`, so tasks only ever see committed data.
- The request's correlation id travels in the task headers, so logs from a request and its tasks can be joined.
- Redis is also the cache used for API rate limiting. It is not used to store business data.

## Consequences

- If Redis is down, business writes still commit and only follow-up work (e.g. a WhatsApp notification) fails to enqueue. That must be surfaced, never hidden.
- Retries and duplicates are safe by construction, backed by idempotency keys and unique constraints.
- Operators run `celery -A config worker` (and `beat` once scheduled jobs exist) alongside Gunicorn.
