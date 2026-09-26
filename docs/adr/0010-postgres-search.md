# 0010: Product search runs on PostgreSQL, not an LLM or a search server

**Status:** Accepted, 2026-09-26 (Stage 5)

## Decision
`apps.search.services.search_products` ranks results in this order:
1. an exact code match (SKU, alternative SKU, barcode, old website id);
2. a full-text match (`simple` config, prefix matching; name weighted above brand/aliases, which are above category), with every word required and synonyms (the `Synonym` table, editable in admin, seeded with trade/Swahili terms) OR-ed per word;
3. a fuzzy match on `word_similarity` against the name (a `pg_trgm` GIN index), which handles typos.

If full-text search errors, it falls back to a case-insensitive name match, so search degrades instead of breaking. Drafts and archived products are never returned. An LLM may later *rewrite* a vague question into a query (Sales Agent), but the results always come from this function.

## Why not Elasticsearch / vectors
At about 1,200 products this runs in milliseconds. There is no separate index that can go stale, and no extra service to operate. Revisit only if measured relevance or latency calls for it.
