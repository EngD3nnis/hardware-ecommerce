# 0006: Catalogue model: Product is the SKU, typed attributes, content-addressed images

**Status:** Accepted, 2026-09-26 (Stage 2)

## Context

The legacy catalogue has 1,160 items, almost all single-SKU. Hardware needs specifications (diameter, pressure class, finish, cylinder type…) that differ by category and keep growing. A rigid schema needs a migration for every new spec. A free-form EAV table becomes unreadable and unvalidated. 52 photos are shared between 107 products.

## Decision

- **`Product` is the sellable SKU.** `sku` is unique, upper-case (a DB check constraint) and never edited after creation. `legacy_id` preserves the static-site id forever, because old WhatsApp links use it. An optional `ProductFamily` groups variants. There is no mandatory Product→Variant split.
- **Common properties are columns** (brand, unit of measure, material, finish, colour, size label, dimensions, barcode). **Category-specific specs are `AttributeDefinition`s** that staff create in admin: typed (TEXT/NUMBER/CHOICE/BOOLEAN), optionally scoped to categories, and validated on write. Each `ProductAttributeValue` holds exactly one typed value (a DB check constraint).
- **Alternative codes** (legacy/alias SKUs, manufacturer parts) live in `ProductIdentifier`. A code may identify only one product across SKUs and identifiers. Search terms live in `ProductAlias`.
- **Images are content-addressed.** A `MediaAsset` is one stored file keyed by SHA-256 (path `products/ab/<sha>.jpg`, cacheable forever), linked to products through `ProductMedia`. The DB enforces at most one primary image per product. Staff uploads are validated and re-encoded, which strips metadata and appended payloads.
- **All writes go through `apps.catalog.services`**, which validate, normalise and write the audit log. Admin, importers, API and (later) AI tools share them.
- **Data quality is surfaced, not silently fixed.** `CatalogReviewItem` holds problems for a person to decide on, keyed by a fingerprint so detection is idempotent. `CatalogChangeProposal` holds a suggested field change that applies only after human approval, and becomes STALE if the field changed in the meantime. AI agents can propose but never approve.

## Consequences

- New specifications need no code or migration. Filtering by attribute is a join, which is acceptable at this catalogue size.
- A product with genuinely different variants is modelled as several products in one family, each with its own SKU, stock and price.
- Legacy links keep working through `legacy_id` (`/api/v1/catalog/products/legacy/?ids=`).
