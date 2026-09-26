# API

- Interactive reference: `/api/v1/docs/`. Machine-readable OpenAPI 3: `/api/v1/schema/`. Both are generated from the code by drf-spectacular and validated in CI, so they cannot drift.
- Versioning: everything is under `/api/v1/`. Breaking changes go to `/api/v2/`, and v1 keeps working until clients move.
- Authentication: public catalogue, business profile and quote-request endpoints need none (rate limited). Staff endpoints (`/inventory/…`, `/analytics/…`) need a JWT (`POST /api/v1/auth/token/`) or a staff admin session.
- Errors: `{"error": {"code", "message", "details", "request_id"}}`. Quote `request_id` when reporting a problem.
- Idempotency: `POST /api/v1/quote-requests/` requires an `Idempotency-Key` header. Retrying with the same key returns the same quotation.
- Money is returned as strings with 2 decimals. Quantities as strings with up to 3.
- Webhooks (not part of the public API): `/webhooks/mpesa/<token>/`, `/webhooks/whatsapp/` (signature verified).
