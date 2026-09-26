# Dewmix Hardware Platform

Digital operating system for Dewmix Hardware (Kenol, Murang'a): catalogue, quotations, inventory, orders, payments, and (later) AI-assisted automation.

The repository has two parts:

| Path | What | Status |
|---|---|---|
| `dewmix_source/` | The **live static website** and WhatsApp quote flow (deployed to Hostinger). | In production. See [its README](dewmix_source/README.md). |
| `backend/` | Django + PostgreSQL system of record (modular monolith). | Being built in stages; not yet serving customers. |

Start with the architecture docs:

- [docs/architecture/current-state.md](docs/architecture/current-state.md): audit of what exists and the issue register
- [docs/architecture/target-state.md](docs/architecture/target-state.md): the target design and staged plan
- [docs/adr/](docs/adr/): why key decisions were made

## Local development

Requirements: Python 3.11, Docker (for PostgreSQL and Redis).

```bash
# 1. Services
docker compose up -d                 # PostgreSQL 16 on :5432, Redis 7 on :6379

# 2. Python environment
python3.11 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

# 3. Optional local config (defaults already match docker-compose)
cp .env.example .env

# 4. Database and admin user
cd backend
../.venv/bin/python manage.py migrate
../.venv/bin/python manage.py createsuperuser   # asks for email + password
../.venv/bin/python manage.py import_legacy_catalog   # 1,160 products + images from dewmix_source/ (~2 min, safe to re-run)

# 5. Run
../.venv/bin/python manage.py runserver         # http://127.0.0.1:8000/admin/
```

Celery worker (needs the settings module exported, since workers default to production settings):

```bash
cd backend
DJANGO_SETTINGS_MODULE=config.settings.local ../.venv/bin/celery -A config worker -l info
```

The static site still runs on its own:

```bash
cd dewmix_source && python3 -m http.server 8080
```

## Tests and checks

Tests run against PostgreSQL (start `docker compose up -d` first):

```bash
cd backend
../.venv/bin/pytest                          # full suite
../.venv/bin/ruff check . && ../.venv/bin/ruff format --check .
../.venv/bin/python manage.py makemigrations --check --dry-run
```

CI (`.github/workflows/ci.yml`) runs all of the above plus `check --deploy` against production settings.

## Configuration

All configuration comes from environment variables; see [.env.example](.env.example) for the full list.

| Settings module | Used by | Behaviour |
|---|---|---|
| `config.settings.local` | `manage.py` default | DEBUG on, dev secret fallback, local Postgres/Redis |
| `config.settings.test` | pytest | Postgres, eager Celery, in-memory cache |
| `config.settings.production` | **default for Gunicorn (wsgi/asgi) and Celery** | Refuses to start unless the requirements below are met |

Production requires (the app **will not start** otherwise; see [ADR 0005](docs/adr/0005-fail-closed-configuration.md)):

- `DJANGO_SECRET_KEY` and `JWT_SIGNING_KEY`: different, random, ≥ 50 characters
- `DJANGO_ALLOWED_HOSTS`: explicit host names, no `*`
- `DATABASE_URL`: PostgreSQL

Strongly recommended in production: `REDIS_CACHE_URL` (shared rate limiting across Gunicorn workers), `CELERY_BROKER_URL`, `SENTRY_DSN`, `METRICS_TOKEN`, `LOG_FORMAT=json` (the default).

## Catalogue

- **Legacy website import:** `manage.py import_legacy_catalog [--dry-run] [--skip-images] [--json]`. Idempotent and non-destructive. It never overwrites a product that exists in the database, and reports differences instead. Data problems it finds (duplicate names or photos, truncated names, SEO words in names, catch-all subcategories) go to **Admin → Catalog review items**. Brand suggestions go to **Catalog change proposals** for approval.
- **Spreadsheet import (xlsx/csv):** Admin → Import batches → Add. The file is validated in the background (dry run) and the batch shows a report: rows accepted, rejected, duplicates, missing SKUs and categories, new categories, suspicious values. Someone with the *approve and import* permission then runs **Approve and import**. That applies all rows in one transaction, and **Roll back** undoes it where safe. Recognised columns are listed in `apps/catalog/imports.py`.
- **Prices:** set on the product page ("New price"). Every change is kept as history. With no price (or with "price on request" ticked) the item is quoted individually. Public prices stay hidden until *Business profile → show prices online* is on.

## Operational endpoints

| Endpoint | Purpose |
|---|---|
| `GET /health/live` | Process is up (no dependency checks). For liveness probes. |
| `GET /health/ready` | Database and Redis broker reachable → 200, otherwise 503. For load balancers. |
| `GET /metrics` | Prometheus metrics. Requires `Authorization: Bearer $METRICS_TOKEN`; disabled if the token is unset. |
| `/admin/` | Django admin (staff only). |

## Public API (no login)

| Endpoint | |
|---|---|
| `GET /api/v1/catalog/products/?q=&category=&brand=` | Active products (paginated: `limit`, `offset`) |
| `GET /api/v1/catalog/products/{sku-or-code}/` | One product by SKU, alternative SKU or barcode |
| `GET /api/v1/catalog/products/legacy/?ids=0,12,45` | Products by old website id, in the order given (old quote links) |
| `GET /api/v1/catalog/categories/`, `/brands/` | Navigation |
| `GET /api/v1/business-profile/` | Contact details, WhatsApp number, opening hours |
| `GET /api/v1/docs/` | API documentation (OpenAPI schema at `/api/v1/schema/`) |

## API conventions

- Versioned under `/api/v1/`. Authentication: JWT (`POST /api/v1/auth/token/`, `/token/refresh/`, `/token/blacklist/` to log out). Auth endpoints are rate limited.
- Every response carries an `X-Request-ID` header. Send your own (e.g. from the reverse proxy) to correlate logs.
- Errors always have this shape; quote the `request_id` when reporting a problem:

```json
{"error": {"code": "not_found", "message": "Not found.", "details": {}, "request_id": "3e6591b9d6664a85b94c92b071060a00"}}
```

## Backend layout

```
backend/
  config/            settings/, urls.py, celery.py, wsgi.py, asgi.py
  apps/
    core/            base models, Actor, BusinessProfile, exceptions + API error handler,
                     request-id middleware, JSON logging, health/metrics views
    audit/           append-only AuditEvent + record()
    authentication/  staff User (email login), JWT endpoints
    catalog/         products, categories, brands, attributes, media, review queue,
                     proposals, legacy + spreadsheet importers, public API
    pricing/         price lists and effective-dated prices
```

The data model is described in [docs/architecture/data-model.md](docs/architecture/data-model.md), and security controls in [docs/architecture/security.md](docs/architecture/security.md).

Business rules go in services (`apps/<domain>/services.py`), not in views, serializers, admin, Celery tasks or AI prompts. See target-state §2.
