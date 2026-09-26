# Security

This page records the security controls that exist today and how to operate them. It is updated in each stage.

## Configuration and secrets

- All secrets come from environment variables ([.env.example](../../.env.example)); none are in the repository.
- Production fails to start without strong, *distinct* `DJANGO_SECRET_KEY` and `JWT_SIGNING_KEY`, explicit `DJANGO_ALLOWED_HOSTS` and a `DATABASE_URL` ([ADR 0005](../adr/0005-fail-closed-configuration.md)).
- **Rotation:**
  - Rotating `JWT_SIGNING_KEY` invalidates all API tokens, so clients log in again.
  - Rotating `DJANGO_SECRET_KEY` invalidates sessions and password-reset links. Django's `SECRET_KEY_FALLBACKS` can phase this in without logging everyone out.
  - Integration credentials (object storage, Sentry, later M-Pesa, WhatsApp and LLM keys) are rotated at the provider, then the environment variable is updated and services are restarted.

## Authentication and authorisation

- Staff use the Django admin (session login) and JWT for API clients. Login, refresh and logout are rate limited per IP (`THROTTLE_RATE_AUTH`, default 10/min). Logout blacklists the refresh token, and a rotated refresh token cannot be reused.
- Public catalogue endpoints are anonymous and read-only, rate limited per IP (`THROTTLE_RATE_PUBLIC`, default 120/min). They never expose drafts, archived products, or prices unless `show_prices_online` is on.
- The API default is `IsAuthenticated`. Public endpoints opt out explicitly.
- Risky admin actions need their own permissions, beyond "can change":
  - `catalog.commit_importbatch`: apply a spreadsheet;
  - `catalog.rollback_importbatch`: roll back a spreadsheet;
  - `catalog.decide_catalogchangeproposal`: approve or reject proposals.
- Products cannot be bulk-deleted from the admin (archive instead). Audit events, price history and import rows are read-only in the admin.

## Data integrity

- Business invariants are enforced by PostgreSQL constraints where possible ([data-model.md](data-model.md)), not only by Python.
- The audit log is append-only via a database trigger ([ADR 0007](../adr/0007-audit-log-append-only.md)). **Production hardening:** the application's database role should not have `TRUNCATE` on `audit_auditevent`, and should not own the table (so it cannot drop the trigger). Migrations run as the owner role.

## Uploads

- Product images must be real JPEG, PNG or WebP files, at most 10 MB and 40 megapixels (checked with Pillow, including a full decode). Staff uploads are re-encoded, which drops EXIF metadata such as GPS location and anything appended to the file (polyglots).
- Spreadsheet imports accept only `.xlsx`/`.csv`, at most 10 MB and 20,000 rows. They are parsed as data only (formulas are read as their cached values), and nothing is applied until a person with `commit_importbatch` approves.
- Stored filenames are content hashes. User-supplied filenames never become storage paths.

## Transport, headers and errors

- Production: HTTPS redirect, HSTS (1 year, preload), secure cookies, `nosniff`, `X-Frame-Options: DENY`.
- API errors use one envelope and never include stack traces or exception text. Unexpected errors are logged with a traceback and a request id.
- CORS: explicit allow-list only (`CORS_ALLOWED_ORIGINS`).

## Privacy

- Sentry runs with `send_default_pii=False`.
- Audit events store the client IP and user agent for accountability; they are visible only to staff with audit view permission.

## Observability endpoints

- `/metrics` requires `Authorization: Bearer $METRICS_TOKEN` and is disabled when the token is unset.
- `/health/*` reveal only ok/error per dependency, never error details.
