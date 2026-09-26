# Current State Audit

**Audited:** 2026-09-26, at commit `b5d8f31` ("Initialize project structure with backend and frontend"), the only commit.
**Method:** every backend source file was read. The static site was parsed programmatically (the files are 4–26 MB, mostly base64). Django was installed into a local `.venv` from `requirements.txt` and `manage.py check`, `check --deploy`, `makemigrations --dry-run`, `migrate` (SQLite) and `test` were run. PostgreSQL and Redis were **not** available locally, so nothing was run against them.

---

## 1. Summary

The repository has two unrelated halves:

| Part | Maturity | Serves customers today? |
|---|---|---|
| `dewmix_source/`: static website + WhatsApp quote flow | Working production site (deployed to Hostinger per its README) | **Yes** |
| `backend/`: Django project | Scaffold: models only. No migrations, views, serializers, admin, services, tasks or tests | No |

The backend is ~450 lines of Python. There is no business logic to preserve in it, only structure and a few settings decisions. The **valuable asset is the static catalogue**: 1,160 products with stable IDs and SKUs, 1,160 photos, 19 categories, 55 subcategories and a working WhatsApp quote flow that customers already use.

That makes the backend migration low-risk (little to break), while the static site must be treated as production and kept working throughout.

---

## 2. Project structure

```
.
├── requirements.txt            # 22 deps, all unpinned (>=)
├── .gitignore
├── backend/
│   ├── manage.py               # defaults to config.settings.local
│   ├── celery_app.py           # Celery app lives OUTSIDE the config package
│   ├── config/
│   │   ├── __init__.py         # `from celery_app import app` (relies on cwd on sys.path)
│   │   ├── urls.py             # /admin, prometheus at '', 7 empty /api/v1/* includes
│   │   ├── wsgi.py / asgi.py   # both default to config.settings.local
│   │   └── settings/{base,local,production}.py
│   └── apps/
│       ├── common/             # UUIDModel, TimeStampedModel (abstract)
│       ├── authentication/     # email-login User (UUID pk), JWT token urls
│       ├── catalog/            # Category, Product, ProductImage
│       ├── inventory/          # Warehouse, Stock
│       ├── orders/             # Order, OrderItem, Invoice
│       ├── payments/           # PaymentTransaction
│       ├── communications/     # WhatsAppLog
│       └── ai_service/         # AIInteractionLog
└── dewmix_source/
    ├── index.html   (4.1 MB) # whole site: HTML+CSS+JS, CATALOG JSON inline, 18 inline base64 images
    ├── products.html (139 KB) # WhatsApp quote landing page; second copy of CATALOG
    ├── images.js    (26 MB)  # const IMAGES = {id: "data:image/jpeg;base64,..."} × 1,160
    ├── wadfow.js    (2.5 MB) # WADFOW_CATALOG (62 items) + base64 images for homepage carousel
    ├── logos.js     (0.5 MB) # 8 partner logos as base64 PNG in an HTML string
    ├── imgs/        (11 MB)  # <id>.jpg × 1,160
    └── README.md
```

No root README, `.env.example`, Dockerfile, CI config, lint/type config or docs existed before this audit.

---

## 3. Backend

### 3.1 Django apps and models

All models inherit `TimeStampedModel` (UUID pk, `created_at`, `updated_at`).

| App | Models | Notes |
|---|---|---|
| `authentication` | `User(AbstractUser)` | `USERNAME_FIELD='email'`, UUID pk. `username` still exists and is set to the email. |
| `catalog` | `Category` (self-FK tree), `Product`, `ProductImage` | `Product.sku` unique, `barcode` unique nullable, `legacy_id` unique nullable (**maps to static-site `id`**). A single `price` + `discount_price`. No brand, variants, attributes, units, aliases or supplier refs. `ProductImage.image_url` is a URLField: no object key, alt text, checksum or variant. |
| `inventory` | `Warehouse`, `Stock(warehouse, product, quantity, low_stock_threshold)` | A mutable integer with no movement history, no reservations and no constraint preventing negatives. |
| `orders` | `Order`, `OrderItem`, `Invoice` | `Order.customer` → `User` (so walk-in/WhatsApp customers without accounts can't be represented). 5 free statuses (`Pending/Paid/Shipped/Cancelled/Returned`), no transition rules, no lifecycle timestamps. `OrderItem` snapshots only `price`, not SKU/name/discount/tax. |
| `payments` | `PaymentTransaction` | `reference_id` unique (a good start on idempotency). No M-Pesa specifics (CheckoutRequestID, MSISDN, receipt no.), no webhook event log. |
| `communications` | `WhatsAppLog` | Free-text status, no direction, no provider message id, no link to customer/quote/order. |
| `ai_service` | `AIInteractionLog` | Provider/model/tokens/cost/purpose. No agent, run, tool call, latency or outcome. |

### 3.2 Migrations: CRITICAL

**No app has a `migrations/` package.** `makemigrations --dry-run` with no labels reports "No changes detected", which hides the problem. Django treats all eight apps as *unmigrated* and `migrate` creates their tables with the legacy `syncdb` path. Consequences:

- The schema isn't versioned, so any model change can't be applied to an existing database.
- `AUTH_USER_MODEL` points at an unmigrated app. This works today only because of a Django fallback, and it is the classic swapped-user-model trap if a database is created before migrations exist.

Verified: running `makemigrations --dry-run` with explicit app labels produces 0001 for every app. Since no production database exists, **now is the cheapest moment to fix this.** Many of these models will be redesigned anyway (see target-state), so initial migrations should be generated *after* the Stage 2 model redesign, not for the current scaffold.

### 3.3 APIs

`/api/v1/auth/token/` and `/token/refresh/` (SimpleJWT) are the only working endpoints. The other six `/api/v1/*` URL modules are empty. DRF defaults: JWT auth, `IsAuthenticated`, limit/offset pagination. There is no OpenAPI schema, no throttling and no exception handler.

### 3.4 Settings

| Setting | Finding | Severity |
|---|---|---|
| `DJANGO_SETTINGS_MODULE` default | `manage.py`, `wsgi.py`, `asgi.py` and `celery_app.py` all default to **`config.settings.local`** (`DEBUG=True`, `ALLOWED_HOSTS=['*']`, `CORS_ALLOW_ALL_ORIGINS=True`). If a production process starts without the env var, it silently runs in debug mode with open CORS. | CRITICAL |
| `SECRET_KEY` | Falls back to a hard-coded `'django-insecure-default-secret-key-3487'`. `SIMPLE_JWT['SIGNING_KEY']` reuses it, so a missing env var means anyone can forge JWTs. `check --deploy` flags it (W009). | CRITICAL |
| JWT blacklist | `ROTATE_REFRESH_TOKENS` + `BLACKLIST_AFTER_ROTATION=True`, but `rest_framework_simplejwt.token_blacklist` is **not** in `INSTALLED_APPS`, so rotated refresh tokens are never invalidated. | HIGH |
| Prometheus | `django_prometheus.urls` mounted at `''`, so `/metrics` is public and unauthenticated. | MEDIUM |
| Sentry | `send_default_pii=True` sends customer IPs/cookies/user data to a third party. This is a privacy concern under Kenya's Data Protection Act 2019. | MEDIUM |
| `DATABASE_URL` default | Falls back to SQLite silently. Postgres-specific behaviour (row locks, constraints) would not be exercised in dev. | MEDIUM |
| `TIME_ZONE='UTC'` | Correct storage choice, but no business timezone (`Africa/Nairobi`) is defined for reports, cut-offs or opening hours. | LOW |
| Security headers | `production.py` is reasonable (HSTS, secure cookies, SSL redirect). `SECURE_BROWSER_XSS_FILTER` is obsolete (harmless). | LOW |
| Logging | No `LOGGING` config, no request/correlation IDs. | MEDIUM |

### 3.5 Celery / Redis

`celery_app.py` sits at `backend/` root and `config/__init__.py` does `from celery_app import app`. This only works when `backend/` is the working directory or on `sys.path`. It breaks for `gunicorn config.wsgi` launched from elsewhere or for `celery -A config`. The app is named `dewmix_platform`. There are no tasks, time limits, `acks_late`, retry policy, beat schedule or result expiry, and `debug_task` uses `print`. Redis is used only as the broker/result backend, with no cache configured.

### 3.6 AI integrations

Settings contain keys for OpenAI, Gemini, Anthropic and a local LLM URL, plus `ACTIVE_AI_PROVIDER='Gemini'`. **No code uses any of them.** Three provider SDKs are installed with no abstraction. `google-generativeai` is Google's legacy SDK (superseded by `google-genai`); this should be confirmed before building on it.

### 3.7 Dependencies

All 22 requirements are `>=` with no upper bound or lockfile. A fresh install today resolved Django 5.2.17, DRF 3.18.1, Celery 5.6.3, `openai` 3.19.2 and `anthropic` 1.8.0. `openai` and `anthropic` have jumped major versions past what the `>=` pins were written against. Builds are not reproducible. `boto3` is present but `django-storages` isn't, so object storage isn't actually wired. `django-extensions`, `python-dotenv` and `django-environ` overlap (environ is the one used).

### 3.8 Tests, quality, CI

0 tests. No pytest, coverage, ruff/flake8, mypy, pre-commit or CI.

---

## 4. Static site (`dewmix_source/`)

### 4.1 Catalogue data (`const CATALOG` in index.html)

Parsed and profiled:

| Fact | Value |
|---|---|
| Products | 1,160, each exactly `{id, sku, cat, sub, name}` |
| IDs | 0–1416 with 257 gaps (deleted items); unique |
| SKUs | `DWX-%04d` of the id, all unique, **deterministic from id** |
| Prices | **None.** The business is quote-based ("prices shown online — we quote fairly") |
| Categories / subcategories | 19 / 55 |
| Subcategory = category (no real sub) | 5 categories (Cleaning Agents, Construction Materials, Farm & Garden, Safety & PPE, Water Pumps) |
| Catch-all "General" subcategories | Taps & Faucets/General (146), Locks/General (84), Floor Drains/General (9) |
| Duplicate names (case-insensitive) | 20 pairs, e.g. `Basin Pillar Tap R 16` ×2. These may be real duplicates or colour variants with lost detail |
| Names appear truncated at ~48 chars | ~62, e.g. `Full Bathroom Accessories Piece Set in Matt Blac` |
| Brand field | None. Brands only appear inside names (e.g. Wadfow, Crown) |
| Attributes (size, material, finish…) | None structured. Embedded in names (`20mm`, `500Ml`, `Antique Brass`) |
| Second copy | `products.html` embeds an identical copy of CATALOG (verified equal). Two sources of truth |

`WADFOW_CATALOG` (wadfow.js) is a **separate** list of 62 items with its own id space (0–61) and no SKUs. Only 1 of its names matches a main-catalogue name. These are products shown on the homepage that can't be quoted by SKU.

### 4.2 Images

- `imgs/<id>.jpg`: 1,160 files, exactly the CATALOG id set (verified), 0.8–23 KB each (median 7 KB), which is low resolution.
- `images.js` `IMAGES[id]`: the same 1,160 ids as base64. **The bytes are not identical to `imgs/`** (0 of 50 sampled match; different encodes of the same photo, e.g. id 0 is 9,225 vs 10,084 bytes). `imgs/` should be the import source, since those are real files and slightly larger.
- **52 groups of byte-identical images across 107 products.** Some are legitimately shared photos (variants), others are probably wrong assignments. These need a review queue, not auto-trust.
- The browser downloads the 26 MB `images.js` to show any category page. This is the site's main performance problem on Kenyan mobile data.

### 4.3 Behaviour to preserve

- **Single quote:** `waHrefSingle(p)` builds a `wa.me/254787151516` link with name, category, SKU and `products.html?ids=<id>`.
- **Bulk quote:** an in-memory `basket` (lost on refresh), and `sendBulkWA()` sends a numbered list plus `products.html?ids=1,2,3`.
- **Deep links:** `index.html#product-<id>`, `index.html?products=<ids>`, `products.html?ids=<ids>`. **These URLs already exist in customers' WhatsApp histories and must keep resolving forever**, and the legacy id is the key.
- **Search:** client-side TF-IDF with prefix/substring fuzzy matching over name+cat+sub (labelled "AI vector search" in comments but it's plain TF-IDF).
- Business details are hard-coded in multiple places: `254787151516` (sales WhatsApp, index + products.html), `254743448862`, "Kenol, Murang'a", `dewmixhardware.com`, and schema.org LocalBusiness JSON-LD with opening hours.

### 4.4 Bugs found

| # | Bug | Severity |
|---|---|---|
| S1 | Deep-link parsers use `filter(n => n > 0)` in both `index.html` (`?products=`) and `products.html` (`?ids=`), so **product id 0 (DWX-0000, Aluminium Bathroom Set) is silently dropped** from bulk quote links and can never be opened via those links. | MEDIUM (customer-visible) |
| S2 | Product names/categories are interpolated into `innerHTML` and HTML attributes unescaped. Harmless while data is hand-authored, but **becomes stored XSS once data comes from the API or an AI catalogue agent.** It must be fixed before the site reads backend data. | LOW now → HIGH later |
| S3 | Quote basket isn't persisted, so refresh or navigation loses it. | LOW |

---

## 5. Cross-cutting assessment

### Technical debt / duplicated logic
- CATALOG duplicated across two HTML files; image data duplicated across `imgs/` and `images.js`; WhatsApp number and message templates duplicated across two files.
- The backend has no duplication yet (there is nothing to duplicate).

### Security concerns (ranked)
1. CRITICAL: settings default to `local` in every entrypoint.
2. CRITICAL: insecure fallback `SECRET_KEY` also used as the JWT signing key.
3. HIGH: JWT blacklist enabled in config but the app is not installed.
4. MEDIUM: public `/metrics`; Sentry PII; no rate limiting on `/auth/token/` (brute force); no webhook verification scaffolding.
5. LOW→HIGH: unescaped HTML in the static site (S2).

No secrets are committed (checked settings and git history: a single commit, no `.env`).

### Scalability concerns
Not a present issue for the backend. For the site: the 26 MB images payload and the whole catalogue inlined into HTML. The fix is object-storage images + an API/JSON feed, not infrastructure.

### Missing functionality (vs. the target brief)
Customers (non-login), brands, variants, attributes, aliases, suppliers, procurement, quotations, carts, fulfilment, pricing/price lists, stock movements/reservations, order state machine, payments integration (M-Pesa), webhooks, idempotency, audit log, search, notifications, agents/tools/approvals, admin customisation, import pipeline, API docs, tests, observability, deployment, backups.

### Architectural risks
- **Pricing has no source today.** The site shows no prices and quotes are priced by a human on WhatsApp. The brief mentions an "existing Excel catalogue", **but it is not in this repository.** Until it's provided, pricing and stock can't be imported. The system must support "price on request" as a normal state, not an error.
- `Order.customer → User` forces every customer to have a login, which contradicts a WhatsApp-first business.
- Multi-provider AI settings with no code invite provider SDK calls being scattered later.

### Migration risks
- Breaking legacy deep links (`#product-<id>`, `?ids=`) would break every quote link customers have ever received. `legacy_id` must be preserved and a redirect/compat layer kept indefinitely.
- Duplicate/truncated names and shared images mean a naïve import would propagate errors, so a review queue is needed.
- The static site is live, so the backend must be introduced *beside* it (the site keeps working off its files until an API-backed build is verified), never as a big-bang switch.

---

## 6. Issue register

| ID | Issue | Severity | Planned stage |
|---|---|---|---|
| C1 | Settings default to `local` (DEBUG, open CORS) in all entrypoints | CRITICAL | 1 |
| C2 | Hard-coded fallback SECRET_KEY, reused for JWT signing | CRITICAL | 1 |
| C3 | No migrations for any app | CRITICAL | 1 (packages) / 2 (initial migrations after redesign) |
| H1 | JWT blacklist app not installed while blacklisting enabled | HIGH | 1 |
| H2 | Unpinned dependencies, no lockfile; major-version drift | HIGH | 1 |
| H3 | `celery_app` import depends on cwd | HIGH | 1 |
| H4 | Inventory is a mutable integer with no history | HIGH | 3 |
| H5 | Order has no state machine or item snapshots; customer must be a User | HIGH | 4 |
| M1 | Public `/metrics` | MEDIUM | 1 |
| M2 | Sentry `send_default_pii=True` | MEDIUM | 1 |
| M3 | No logging config / request IDs | MEDIUM | 1 |
| M4 | Silent SQLite fallback | MEDIUM | 1 |
| M5 | No auth rate limiting | MEDIUM | 1 |
| M6 | Static site: product id 0 dropped from bulk links (S1) | MEDIUM | 1 (small, isolated fix) |
| M7 | 52 duplicate-image groups, 20 duplicate names, ~62 truncated names | MEDIUM | 2 (review queue) |
| L1 | Static site unescaped HTML (S2), which rises to HIGH before the site consumes API data | LOW | 5/7 |
| L2 | Quote basket not persisted | LOW | 5 |
| L3 | No business timezone | LOW | 1 |
