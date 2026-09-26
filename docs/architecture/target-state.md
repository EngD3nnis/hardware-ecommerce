# Target State Architecture

**Status:** Proposed (2026-09-26). Nothing here is implemented yet. It is built on the findings in [current-state.md](current-state.md). Each significant decision gets an ADR in `docs/adr/` when its stage is implemented.

## 0. Guiding rules (short form)

1. **Django + PostgreSQL are the system of record.** AI, WhatsApp, search indexes and the static site are consumers or channels.
2. **Business logic lives in services** (`apps/<domain>/services.py`), not in views, serializers, admin, tasks or prompts. Views, admin, Celery tasks and AI tools are all thin callers of the same services.
3. **Every consequential write is transactional, auditable and idempotent.**
4. **Everything optional can be switched off** (agents, automations, search index, notifications) and the shop still runs by hand.
5. **Boring technology first.** Every addition below answers "what problem does Dewmix have *now* that Django/Postgres/Redis/Celery can't solve?"

---

## 1. Runtime topology

```
                 ┌───────────── Internet ─────────────┐
                 │                                    │
        Static site (Hostinger, today)        WhatsApp (wa.me links today;
        → later served from Django/CDN          Cloud API later)
                 │                                    │
                 ▼                                    ▼
          ┌──────────────── reverse proxy (Caddy) ───────────────┐
          │                                                      │
          ▼                                                      │
   Django/Gunicorn  ── /api/v1/*, /admin, /ops (control centre),  │
          │            /webhooks/*, legacy-link compat routes     │
          │                                                      │
   ┌──────┼───────────────┬──────────────────┐                   │
   ▼      ▼               ▼                  ▼                   │
PostgreSQL  Redis (broker + cache)   Object storage (S3-compatible: product images,
 (truth)        │                      documents, backups)
                ▼
        Celery worker(s) + Celery beat
        (imports, notifications, indexing, scheduled checks, agent runs)
                │
                ▼
        LLM providers (via AIProvider abstraction only)
```

A single VPS running Docker Compose is enough: `caddy`, `web`, `worker`, `beat`, `redis`, plus Postgres (managed if affordable, otherwise a container with off-site backups). **Not planned:** Kubernetes, Kafka, Elasticsearch, microservices or a vector DB. Section 9 gives the reasons.

---

## 2. Code layout

Modular monolith. One Django project, apps by business domain, each with the same shape:

```
backend/
  config/                 settings/{base,local,test,production}.py, urls.py, celery.py
  apps/
    core/          # (renamed from common) base models, exceptions, idempotency, request-id middleware, money utils
    accounts/      # staff/admin User (email login), roles/groups
    customers/     # Customer (NOT a login), contacts, addresses, channel identities (WhatsApp number)
    catalog/       # Category, Brand, ProductFamily, Product(=SKU), attributes, aliases, media, import pipeline
    pricing/       # PriceList, ProductPrice (effective-dated), "price on request"
    inventory/     # StockLocation, StockBalance, StockMovement (ledger), Reservation
    suppliers/     # Supplier, SupplierProduct (supplier SKU, cost, lead time)
    procurement/   # PurchaseOrder, PurchaseOrderLine, GoodsReceipt
    quotations/    # Quotation, QuotationLine, conversion to Order
    orders/        # Order, OrderLine (snapshots), OrderEvent (state transitions)
    payments/      # Payment, InboundPaymentEvent (webhooks), M-Pesa adapter
    fulfillment/   # Shipment/Pickup, pick lists
    notifications/ # Outbound messages + channel adapters (WhatsApp, SMS, email)
    search/        # query parsing, Postgres FTS/trigram, synonyms
    audit/         # AuditEvent (append-only)
    automation/    # kill switch, ApprovalRequest, scheduled jobs registry, incidents
    ai/            # providers/, tools/, agents/, AgentRun, ToolCall, usage/cost
    analytics/     # aggregates, reports (Stage 10)
```

Carts are folded into `quotations` (a draft quotation *is* the basket for a quote-based business). A separate `carts` app is added only if fixed-price online checkout arrives.

Per-app convention: `models.py`, `services.py` (write operations), `selectors.py` (read queries), `api/` (serializers + views), `admin.py`, `tasks.py` (thin), `tests/`. Services take explicit arguments, including an **`actor`** (who is acting: user / system / agent / integration), and raise typed exceptions from `core.exceptions`.

---

## 3. Domain designs

### 3.1 Catalogue

**Decision: `Product` is the sellable SKU. `ProductFamily` optionally groups variants.** Almost all 1,160 existing items are single-SKU. A mandatory Product→Variant split would double every query and admin screen for little gain. Where a family exists (e.g. *Basin Pillar Tap R-series*, or a PPR pipe in 20/25/32 mm), products share a `family` FK and differ by attribute values. *(ADR-00x)*

```
Category (tree: category → subcategory; slug; sort order)
Brand
ProductFamily (name, brand, category, description)           optional
Product
  sku (unique, internal, e.g. DWX-0000)    legacy_id (unique; the static-site id; never reused)
  name, slug, description, status (DRAFT/ACTIVE/ARCHIVED)
  category FK, brand FK, family FK (null)
  unit_of_measure (piece, metre, roll, kg, litre, box, set…)
  common structured fields: material, finish, colour, size_label
  dimensions: length_mm, width_mm, height_mm, weight_g (nullable)
  barcode/GTIN (unique nullable)
  price_on_request (bool)
ProductIdentifier (product, kind=[SUPPLIER|LEGACY|ALIAS_SKU|GTIN], value, supplier FK?) unique(kind, value, supplier)
ProductAlias      (product, text): search synonyms like "PPR" ↔ "polypropylene", Swahili/trade names
AttributeDefinition (code, label, data_type=[TEXT|NUMBER|CHOICE|BOOLEAN], unit, choices, category scope)
ProductAttributeValue (product, attribute, value_text / value_number / value_choice)  unique(product, attribute)
ProductMedia (product, variant-level via product, object_key, sha256, width, height, alt_text, sort, is_primary)
             unique(product, sha256); partial unique(product) where is_primary
ProductDocument (datasheets, manuals)
```

That is controlled flexibility: common fields are real columns (filterable, readable). Category-specific specs (PPR *pressure class*, lock *cylinder type*) are `AttributeDefinition` rows admins create **without migrations**, typed and validated, and scoped to categories. It is not open-ended EAV: values must match a definition.

**Legacy link compatibility (permanent):** Django serves `/products.html?ids=…`, `/#product-<id>` (JS shim) and `/?products=…` by resolving `legacy_id`, so every quote link ever sent keeps working. Deleted products return an "item no longer available — ask us" page, not a 404.

### 3.2 Pricing

Dewmix is quote-first. So:
- `PriceList` (e.g. *Retail*, *Contractor*), `ProductPrice(product, price_list, amount KES, valid_from, valid_to)`. Effective-dated, never overwritten, so history is automatic.
- `price_on_request` is a first-class state. The Sales Agent must say "price on request" and create a draft quote for a human to price, never guess.
- Money: `DecimalField(max_digits=12, decimal_places=2)`, currency KES. VAT handling is configurable (whether Dewmix is VAT-registered is an open question, see §10).

### 3.3 Inventory (ledger-based)

```
StockLocation (shop floor, store room, … ; kind)
StockMovement  (append-only ledger)
  product, location, quantity_delta (signed), bucket=[ON_HAND|DAMAGED]
  reason = PURCHASE_RECEIVED | SALE | RETURN | DAMAGE | TRANSFER_IN | TRANSFER_OUT
         | MANUAL_ADJUSTMENT | STOCKTAKE_CORRECTION
  source_type/source_id (e.g. GoodsReceipt 12, Order 88), actor, note
  idempotency_key UNIQUE
StockBalance   (product, location) UNIQUE: on_hand, reserved, damaged
  CHECK on_hand >= 0, reserved >= 0, reserved <= on_hand, damaged >= 0
Reservation    (product, location, quantity, order/quotation, status=ACTIVE|RELEASED|CONSUMED, expires_at)
```

- `available = on_hand − reserved` (damaged stock is held in its own bucket, not in `on_hand`). `incoming` is computed from open purchase-order lines and never stored.
- **Only `InventoryService` writes** movements, balances and reservations. Each operation runs in `transaction.atomic()`, locks the affected `StockBalance` rows with `select_for_update()` **in a stable order (by product id, location id) to avoid deadlocks**, appends the movement and updates the balance in the same transaction. The DB `CHECK` constraints are the last line of defence against negative stock.
- Reservations are separate from movements: reserving doesn't change `on_hand`, and consuming a reservation on dispatch writes a `SALE` movement.
- A nightly `reconcile_inventory` task recomputes balances from the ledger and raises an incident on mismatch (it never auto-corrects).
- Negative available stock is disallowed by default. A per-location `allow_backorder` flag is the explicit business-rule escape hatch.

*Why not a single mutable integer?* Because the question "why is this 43 and not 50?" must always have an answer. *(ADR)*

### 3.4 Customers

`Customer` is separate from login `User`. Fields: name, kind (individual/contractor/business), phone numbers (E.164, normalized, the WhatsApp identity), email, addresses, KRA PIN (optional, for invoices), notes, consent flags. A later customer portal links `User` → `Customer`; checkout never requires an account.

### 3.5 Quotations (the core of today's business)

```
Quotation: number (Q-2026-000123), customer, status, channel (WEB|WHATSAPP|WALK_IN|AGENT),
           valid_until, notes, created_by (actor), source_message ref, idempotency_key
QuotationLine: product, qty, unit_price (nullable until priced), discount, snapshot of sku/name/unit
Status: DRAFT → PRICED → SENT → ACCEPTED → CONVERTED
                            ↘ REJECTED / EXPIRED (beat task)
```

- The website basket becomes a **draft quotation via the API** (the WhatsApp message then carries the quote number instead of a raw id list, and the old id-list links keep working).
- `QuotationService.convert_to_order(quote, actor)`: one transaction creates the order with copied snapshot lines, reserves stock and marks the quote CONVERTED. It is idempotent because a quote can only convert once (unique FK on Order).

### 3.6 Orders

State machine in plain code (a transitions dict + `OrderService.transition(order, to, actor, reason)`), with no third-party FSM library:

```
DRAFT → CONFIRMED → PAYMENT_PENDING → PAID → ALLOCATED → PICKING → PACKED → DISPATCHED → DELIVERED
  any pre-DISPATCHED state → CANCELLED (releases reservations; if PAID → requires REFUND or explicit credit decision)
  DELIVERED → (return workflow) → REFUNDED / PARTIALLY_REFUNDED
  ALLOCATED/PICKING → PARTIALLY_FULFILLED (only via fulfilment service)
  CONFIRMED → ALLOCATED allowed when payment terms = on account / cash on collection
```

- `status` is not editable in admin forms. Only service actions can change it.
- `OrderEvent(order, from_status, to_status, actor, reason, at, correlation_id)`, plus lifecycle timestamps on Order (`confirmed_at`, `paid_at`, `dispatched_at`…).
- `OrderLine` stores snapshots: product FK (PROTECT), sku, name, unit, attributes JSON, unit_price, qty, discount, tax, line_total. Invoices render from snapshots only.
- The model `QUOTED` status from the brief is represented by the Quotation entity rather than an order status, which avoids two objects meaning the same thing.

### 3.7 Payments

- `Payment(order, method, provider_reference UNIQUE, amount, status, received_at)`.
- `InboundPaymentEvent(provider, event_id UNIQUE, payload, signature_ok, processed_at, error)`: **every webhook is stored first, then processed.** Duplicate deliveries hit the unique constraint and are acknowledged without reprocessing.
- M-Pesa (Daraja STK Push / C2B) is the first adapter, behind a `PaymentProvider` interface. Callback source is verified (IP allow-list + callback-URL secret token, since Daraja callbacks aren't signed).
- Manual payment recording (cash, bank transfer) goes through the same `PaymentService.record_payment`.

### 3.8 Suppliers & procurement

`Supplier`, `SupplierProduct(supplier, product, supplier_sku, last_cost, lead_time_days, moq)`, `PurchaseOrder` (DRAFT → PENDING_APPROVAL → APPROVED → SENT → PARTIALLY_RECEIVED → RECEIVED / CANCELLED), `GoodsReceipt` → `InventoryService.receive()`. The approval threshold (KES) is a setting. The Inventory Agent can only create DRAFT purchase orders.

### 3.9 Fulfilment

`Fulfillment(order, method=PICKUP|DELIVERY, status, assigned_to, pick list, dispatched_at, delivered_at, proof)`. It drives order transitions through `OrderService`.

### 3.10 Notifications / WhatsApp

- `OutboundMessage(customer, channel, template, body, status, provider_message_id, idempotency_key UNIQUE, related object, attempts, last_error)`.
- `ChannelAdapter` interface → `WaMeLinkAdapter` (today: generates click-to-chat links, sends nothing), later `WhatsAppCloudAdapter`, `SmsAdapter` (e.g. Africa's Talking), `EmailAdapter`.
- `BusinessProfile` (singleton in DB, editable in admin): business name, WhatsApp numbers, address, map link, opening hours. **Nothing hard-codes `254787151516`.** The static site export pulls from here.
- Sending always goes through a Celery task with retries. Failure marks the message FAILED and raises an operations alert. The order/quote is never affected.

### 3.11 Search

**PostgreSQL full-text search + `pg_trgm`**, not Elasticsearch:
- Exact SKU / legacy id / barcode / identifier match first (deterministic, ranked top).
- Then a weighted `SearchVector` (name A, aliases/brand B, category/attributes C) + trigram similarity for misspellings ("padlok", "ppr pipe 20").
- A synonyms table (e.g. `GI ↔ galvanised iron`, `PPR ↔ polypropylene`, `tap ↔ faucet`, Swahili/trade terms) is expanded before querying.
- A search vector column is maintained by a DB trigger or on save (no external index to go stale). If FTS errors, fall back to `icontains` on name/SKU.
- An LLM may *rewrite* a vague query into structured filters (Sales Agent only). The results always come from Postgres.

At ~1–20k SKUs Postgres is fast enough. Revisit only if measured latency says so.

### 3.12 Audit

`AuditEvent(at, actor_type=USER|ADMIN|SYSTEM|AGENT|INTEGRATION, actor_id, actor_label, action, object_type, object_id, before JSON, after JSON, reason, correlation_id, ip, user_agent)`.
Services write it explicitly (no magic signals). Append-only is enforced **in the database**: a migration adds a trigger rejecting UPDATE/DELETE on the table. Admin is read-only with no delete permission. Stock movements, order events and approvals are themselves ledgers and reference the correlation id.

### 3.13 Idempotency

- `core.IdempotencyKey(scope, key, request_hash, response, created_at)` for mutating API endpoints that accept `Idempotency-Key`.
- Natural unique constraints wherever possible: `InboundPaymentEvent(provider,event_id)`, `Payment.provider_reference`, `StockMovement.idempotency_key`, `OutboundMessage.idempotency_key`, `Order.source_quotation` (one-to-one), `ImportRow(batch, row_hash)`.
- Celery tasks receive ids (not objects), re-read state and no-op if already done.

---

## 4. AI & automation layer

### 4.1 Shape

```
apps/ai/
  providers/   base.py (AIProvider protocol: complete(messages, tools, schema) → AIResult w/ usage)
               anthropic.py, openai.py, gemini.py, fake.py (deterministic, for tests)
  tools/       registry.py (Tool: name, tier, input schema (Pydantic), output schema, handler→service)
               catalog_tools.py, inventory_tools.py, quotation_tools.py, ops_tools.py …
  agents/      definitions.py: SALES, INVENTORY, CATALOGUE, OPERATIONS
               runner.py: the loop: budget/timeout checks, tool dispatch, validation, logging
  models.py    AgentConfig (enabled, model, limits), AgentRun, ToolCall, UsageRecord
apps/automation/
  models.py    AutomationSwitch (global kill switch), ApprovalRequest, Incident
```

- **Provider abstraction:** `settings.AI_PROVIDERS` maps agent → (provider, model). Only `providers/*` imports SDKs. Changing provider is a config change.
- **Agent definitions are code (reviewed, versioned). Their on/off switch, model and budgets are DB config (operable from the control centre).** Each definition declares: purpose, allowed tools, max permission tier, system prompt file, timeout, max steps, max tokens/cost per run and per day, retry policy, escalation target.
- **Tools** are the only way an agent touches Dewmix. A tool validates its input with Pydantic, checks the agent's tier and allow-list, calls a service with `actor=Agent(<name>, run_id)`, and returns structured output. There is no SQL tool, no shell tool and no filesystem tool, ever.
- **Tier enforcement is in the runner and in the tool**, not in the prompt:
  - LOW (read/draft/summarise): executes directly.
  - MEDIUM (draft quotes, internal tasks, alerts, *pre-approved* notification templates): executes directly, audited.
  - HIGH (prices, stock adjustments, cancellations, PO approval, refunds): the tool returns `ApprovalRequest` (PENDING_APPROVAL) and a human approves in admin/control centre, which executes via the same service.
  - CRITICAL: no tool exists. It is impossible to call, not merely forbidden.
- **Output validation:** structured outputs are parsed into Pydantic models. On failure the runner retries a bounded number of times, then marks the run FAILED, logs it and falls back to escalation.
- **Kill switch:** `AutomationSwitch.all_disabled` (DB, cached ≤10 s) plus the env `AUTOMATION_HARD_DISABLE=1` (works even if the DB flag can't be flipped). It is checked at every agent run start and every tool dispatch. The core app never reads it.
- **Cost:** every provider call writes `UsageRecord(agent, run, provider, model, input/output tokens, latency_ms, est_cost, ok)`. Daily budget exceeded → the agent auto-disables and an incident is raised.

### 4.2 The four agents (initial scope)

| Agent | Trigger | Max tier | Tools (initial) |
|---|---|---|---|
| Sales | inbound WhatsApp/web chat (Stage 9) | MEDIUM | search_products, get_product, check_availability (returns in/low/out, not raw numbers unless staff), get_price (returns "on request" when none), get_or_create_customer, create_quote_draft, add_quote_line, escalate_to_human |
| Inventory & Procurement | beat: daily | MEDIUM (+HIGH via approval) | get_stock_levels, get_sales_velocity, get_open_pos, create_inventory_alert, create_po_draft |
| Catalogue/Data | on import batch / on demand | MEDIUM (drafts only) | get_product, find_similar_products, propose_product_change (creates a `CatalogChangeProposal`, never edits), propose_image_match |
| Operations | beat: hourly + daily report | MEDIUM | list_stuck_orders, list_failed_messages, list_failed_payments, list_failed_tasks, create_incident, create_internal_task, get_sales_summary |

Deterministic checks (low stock, stuck orders, failed tasks) are **plain Python queries run by beat**, not LLM calls. The LLM is only invoked to summarise, prioritise or draft language. Each agent can be disabled and the equivalent admin list views still show the humans the same information.

### 4.3 Control centre (`/ops/`, staff-only Django views + admin)

Agent status/toggles, recent runs (steps, tool calls, tokens, cost), failures, pending approvals (approve/reject with reason, showing proposed vs. final values), open incidents, and the big red **Disable all automations** button. It uses plain server-rendered Django templates (no SPA) to keep maintenance cheap.

---

## 5. Cross-cutting

| Concern | Approach |
|---|---|
| Settings | `DJANGO_SETTINGS_MODULE` **required** in production entrypoints; no insecure fallbacks; `production.py` raises `ImproperlyConfigured` if `SECRET_KEY`/`DATABASE_URL` are missing. Separate `JWT_SIGNING_KEY`. `.env.example` documents every variable. |
| Dependencies | `requirements.in` (direct, with compatible ranges) → `pip-compile` → pinned `requirements.txt` with hashes. Dev deps in `requirements-dev.in`. |
| Errors | `core.exceptions`: `DomainError` → `ValidationError`, `NotFound`, `Conflict`, `PermissionDenied`, `InvariantViolation`. A DRF exception handler maps them to `{"error": {"code", "message", "details", "request_id"}}` and never includes stack traces. Bare `except:` is banned (ruff rule). |
| Logging | JSON logs (stdlib `logging` + `python-json-logger`), `RequestIDMiddleware` (accepts/propagates `X-Request-ID`), `correlation_id` contextvar carried into Celery task headers and AuditEvents. |
| Metrics / health | django-prometheus on an internal path, protected by basic-auth/IP allow-list. `/health/live` (process up) and `/health/ready` (DB + Redis reachable). Celery: `task_track_started`, time limits, failure → Sentry + FailedTask log. |
| Sentry | `send_default_pii=False`, scrub phone numbers/emails, environment + release tags. |
| Auth | Staff: Django sessions for admin/ops; JWT for API clients. Token blacklist app installed. Throttling on auth, quote-creation and webhook endpoints (DRF throttles on Redis cache). Public catalogue read endpoints anonymous and cached. |
| API | `/api/v1/`, DRF + `drf-spectacular` (OpenAPI at `/api/v1/schema/`, docs in `docs/api/`). Breaking changes → `/api/v2/`. |
| Uploads | Pillow verification of real image type, size caps, re-encode to WebP/JPEG, sha256 dedupe, stored in object storage via `django-storages`. |
| Celery | `config/celery.py`, `acks_late=True`, `task_reject_on_worker_lost`, soft/hard time limits, `autoretry_for` only on transient errors with backoff, idempotent tasks. **Transactional work happens in services inside the request/transaction;** tasks are dispatched with `transaction.on_commit`. |
| Graceful degradation | AI down → agents fail, humans continue. Redis down → requests work (cache falls back), async jobs can't enqueue and those writes fail loudly; business data is not affected. Search trigram error → `icontains` fallback. Notification failure → message FAILED + alert; the order is intact. |
| Timezone | Store UTC; `BUSINESS_TIMEZONE='Africa/Nairobi'` for reports, expiry, opening hours. |

---

## 6. Testing strategy

`pytest` + `pytest-django` + `factory_boy` + `hypothesis`, against **PostgreSQL** (docker service locally and in CI). Concurrency and constraint tests are meaningless on SQLite.

- **Unit:** pricing selection, availability math, order transition table, permission tiers, schema validation.
- **Integration:** quote → order conversion, reservation/deduction, payment webhook (+duplicate delivery), import dry-run/commit, search ranking fixtures, Celery tasks run eagerly, AI tools with `FakeProvider`.
- **Concurrency:** two threads reserving the last unit, where exactly one must succeed.
- **Security:** anonymous/staff/agent access matrix per endpoint; agent calling a HIGH tool → approval, not execution; tool not in allow-list → refused; webhook without valid token → rejected; malicious upload (polyglot, oversize) → rejected.
- **Property/invariant (hypothesis):** random sequences of receive/reserve/release/consume/damage never violate `0 ≤ reserved ≤ on_hand`, and the ledger sum equals the balance; order transitions never leave the legal graph; duplicate webhook → one payment.
- **CI:** GitHub Actions: ruff, mypy (services/tools typed), `makemigrations --check`, pytest with coverage, `check --deploy` using production settings.

---

## 7. Data migration from the static site

1. **`import_legacy_catalog` management command** reads `CATALOG` directly out of `dewmix_source/index.html` (single parser, file hash recorded). It creates categories/subcategories, then products with `legacy_id=id`, `sku` as given (`DWX-%04d`), status ACTIVE and `price_on_request=True`. It is idempotent: it upserts by `legacy_id`, never deletes, and reports rows created/updated/unchanged/rejected.
2. **`import_legacy_images`** takes `imgs/<id>.jpg` (not `images.js`, since those are separate encodes), matches by filename → `legacy_id`, dedupes by sha256 and uploads once. The **52 duplicate-content groups go into a review queue** (`MediaReviewItem`) instead of being silently assigned.
3. **Data-quality queue:** 20 duplicate-name pairs, ~62 likely-truncated names, the "General" catch-all subcategories and brands embedded in names become `CatalogChangeProposal`s for human review (later assisted by the Catalogue Agent).
4. **Excel catalogue** (when provided): upload → `ImportBatch` → per-row parse/normalise/validate → **dry-run report** (processed/accepted/rejected/duplicates/missing SKU/category/image/suspicious values) → staff approval → commit in a transaction. Rollback: each batch records the created/changed objects (before-values), and an "undo batch" service reverts them if nothing downstream references them.
5. **The static site keeps running unchanged** until Stage 5. Then a `export_static_catalog` command regenerates `CATALOG` JSON + image URLs from the DB. Later the site fetches `/api/v1/catalog/` and loads images from object storage (removing the 26 MB `images.js`). Legacy links are served by Django compat routes.
6. **Wadfow carousel (62 items, no SKUs)** is reconciled by hand in admin (link to existing products or create new ones). There is only one name match, so no automated matching.

---

## 8. Staged delivery

Each stage leaves `main` deployable and the static site working.

| Stage | Deliverables | Exit criteria |
|---|---|---|
| **1. Stabilise** | Fix C1–C3 (migration packages), H1–H3, M1–M5, L3; `core` exceptions/error handler, request-id + JSON logging, health endpoints; pinned deps; pytest+Postgres+CI skeleton; `.env.example`, root README, docker-compose for dev; ADRs 001–005; static-site fix S1 (id 0 dropped). | CI green; `check --deploy` clean under production settings; app boots on Postgres in compose; tests for settings guards & health. |
| **2. Canonical catalogue** | Catalogue + pricing models (§3.1–3.2), initial migrations, admin, legacy catalogue + image importers, review queues, `BusinessProfile`, read-only catalogue API. | All 1,160 products & images imported idempotently (re-run = 0 changes); legacy-id lookup works; import report produced. |
| **3. Inventory** | §3.3 in full, stock admin, stocktake flow, reconciliation task. | Concurrency + hypothesis invariant tests pass. |
| **4. Customers/quotes/orders/payments** | §3.4–3.7, audit app, idempotency, M-Pesa adapter (sandbox). | Quote→order→payment→reservation end-to-end test; duplicate webhook test. |
| **5. Search + site integration** | §3.11, synonyms, basket→draft quote API, static-site export, legacy compat routes, HTML-escaping fix (S2). | Search fixture suite; old deep links verified. |
| **6. Fulfilment** | §3.9. | Dispatch consumes reservations; ledger correct. |
| **7. Notifications/WhatsApp** | §3.10 adapters, templates, retries. | Failed send never affects orders; retry idempotent. |
| **8. AI tools** | providers, tool registry, tiers, approvals, usage tracking, kill switch, control centre. | Permission-bypass tests; FakeProvider-driven tool tests. |
| **9. Agents** | the four agents, one at a time, each shipped disabled-by-default. | Each agent can be disabled with zero loss of manual capability. |
| **10. Analytics/automation** | sales/stock aggregates, reports, dead-stock, demand. | — |

Docs from the brief (`data-model.md`, `ai-agents.md`, `security.md`, `docs/operations/*`, `docs/api/`) are written in the stage that implements what they describe, so they document reality rather than intent.

---

## 9. Deliberately *not* doing (and when to revisit)

| Not adopting | Because | Revisit when |
|---|---|---|
| Microservices | One team, one DB, strong transactional needs across stock/orders/payments | A component needs independent scaling *and* a separate team |
| Kubernetes | A single VPS + compose covers the load; k8s adds a big ops burden | Multiple nodes needed for availability SLAs |
| Kafka / event bus | Celery + Postgres ledger tables give durable events at this scale | Many independent consumers of the same event stream |
| Elasticsearch/OpenSearch | Postgres FTS+trigram handles ~20k SKUs; there is no index to go stale | Measured search latency/relevance problems Postgres can't fix |
| Vector DB / embeddings search | Catalogue is small and structured; lexical + synonyms first | Semantic search measurably beats FTS on real queries (pgvector would be the first step) |
| FSM library, generic workflow engine | A transitions dict is 20 readable lines | — |
| SPA admin | Django admin + a few server-rendered ops pages | — |

---

## 10. Open questions (need business input)

1. **Where is the Excel catalogue?** It's referenced in the brief but not in the repo. It's needed for prices, stock and supplier data.
2. **Pricing:** single retail price list, or contractor/trade tiers? Is Dewmix VAT-registered (16%)? Should prices ever be shown online?
3. **Payments:** Is M-Pesa via Daraja (own Paybill/Till) the priority? Is there an existing Paybill/Till number?
4. **WhatsApp:** keep click-to-chat (`wa.me`) only, or move to the WhatsApp Business Cloud API (needed for the Sales Agent to reply automatically)?
5. **Locations:** one shop in Kenol, or several stock locations/branches?
6. **Hosting:** stay on Hostinger for the static site? Where will Django run (a VPS provider preference)?
7. **Default LLM provider/budget:** which provider to start with and a monthly AI spend ceiling.
8. **Who are the staff users and their roles** (owner, shop attendant, storekeeper, accounts)? This defines the permission groups.
