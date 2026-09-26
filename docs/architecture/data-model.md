# Data Model

The authoritative list is the models themselves (`backend/apps/*/models.py`). This page explains how they fit together and which rules the **database** enforces, as opposed to Python.

Conventions:
- Primary keys are UUIDs (`core.TimeStampedModel`), except append-only ledgers and high-volume rows, which use big integers so their ids sort by insertion order.
- Money is `Decimal(12,2)`, currency KES. It is never a float, and APIs return it as a string.
- Timestamps are stored in UTC. Business-facing times use `settings.BUSINESS_TIMEZONE` (Africa/Nairobi).
- Writes go through each app's `services.py`, which also records `audit.AuditEvent`s.

## core

| Model | Purpose | DB-enforced rules |
|---|---|---|
| `BusinessProfile` | The single row of business contact details and customer-facing settings (WhatsApp number, address, hours, `show_prices_online`, quote validity). Seeded by migration from the static site. | `id = 1` only (singleton check) |

## audit

| Model | Purpose | DB-enforced rules |
|---|---|---|
| `AuditEvent` | Who did what to which object, before/after values, reason, correlation id, IP. | UPDATE/DELETE rejected by trigger (ADR 0007) |

## catalog

```
Category (tree: category → subcategory)
   ▲ category (PROTECT)
Product ──── brand ──► Brand
   │  └──── family ──► ProductFamily (optional variant grouping)
   ├─< ProductIdentifier   legacy/alias SKUs, manufacturer part numbers
   ├─< ProductAlias        extra search terms (e.g. "kufuli")
   ├─< ProductAttributeValue ──► AttributeDefinition (typed spec, scoped to categories)
   ├─< ProductMedia ──► MediaAsset (one stored file per unique image, by SHA-256)
   ├─< ProductDocument
   ├─< CatalogChangeProposal   suggested field change awaiting human approval
   └─< (M2M) CatalogReviewItem data-quality problems for a person

ImportBatch ─< ImportRow   spreadsheet import: validate → approve → commit → rollback
```

| Rule | Enforced by |
|---|---|
| SKU unique, upper-case, non-empty | unique index + check constraints |
| `legacy_id` unique (the static-site id; old WhatsApp links use it) | unique index |
| Barcode unique when present | unique index (NULLs allowed) |
| A code identifies one product across SKUs and identifiers | service (`_check_code_is_free`) |
| Identifier values upper-case and unique per kind | check + unique constraint |
| Category name unique per parent (and among top-level categories) | unique constraints (incl. a partial one for `parent IS NULL`) |
| Attribute value has exactly one typed value | check constraint |
| One attribute value per product per attribute | unique constraint |
| Attribute type, choices and category scope match | service |
| At most one primary image per product | partial unique index |
| An image is stored once | `MediaAsset.sha256` unique |
| One pending proposal per product/field/source | partial unique index |
| Review items not duplicated by re-running detection | `fingerprint` unique |

Product lifecycle: `DRAFT` (hidden) → `ACTIVE` (visible, sellable) → `ARCHIVED` (kept for history). Products are archived, never deleted from the admin. Deletion only happens when rolling back an import that created the product, and only if nothing references it.

## pricing

| Model | Purpose | DB-enforced rules |
|---|---|---|
| `PriceList` | e.g. Retail (default, seeded), Contractor. Currency, whether prices include VAT. | only one `is_default` (partial unique) |
| `ProductPrice` | Effective-dated price of a product on a price list. The history is never edited; a new price closes the old one. | amount > 0; `valid_to > valid_from`; **no overlapping periods per product+list** (exclusion constraint, `btree_gist`) |

"Price on request" is a normal state: no current price, or `Product.price_on_request = True`. Prices appear in the public API only when `BusinessProfile.show_prices_online` is on.
