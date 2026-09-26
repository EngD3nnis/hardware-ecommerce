"""Product search on PostgreSQL. Deterministic; no LLM involved (ADR 0010).

Ranking, best first:
1. exact code match: SKU, alternative/legacy SKU, barcode, old website id;
2. full-text match on name (weight A), brand + aliases (B), category (C),
   with synonyms expanded;
3. fuzzy trigram word-similarity on the name, for misspellings ("padlok").
If full-text search fails for any reason, a plain case-insensitive name match
is used, so search degrades instead of breaking.
"""

import logging
import re

from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector, TrigramWordSimilarity
from django.db import DatabaseError, transaction
from django.db.models import Case, F, FloatField, Q, QuerySet, Value, When
from django.db.models.functions import Coalesce

from apps.catalog.models import Product, normalise_sku
from apps.catalog.selectors import product_by_code, public_products

from .models import Synonym

logger = logging.getLogger(__name__)

MAX_QUERY_LENGTH = 100
TRIGRAM_THRESHOLD = 0.5  # word_similarity: how well the query matches some part of the name
DEFAULT_SYNONYMS = [
    "tap, faucet, mfereji",
    "padlock, kufuli",
    "pipe, piping, bomba",
    "gi, galvanised, galvanized",
    "ppr, polypropylene",
    "pvc, upvc",
    "toilet, wc, water closet",
    "basin, sink",
    "sealant, silicone",
    "adhesive, glue",
    "brush, broom",
    "mixer, shower mixer",
]


def expand_terms(query: str) -> list[str]:
    words = [w for w in re.split(r"[^\w]+", query.lower()) if w]
    groups = [s.term_list for s in Synonym.objects.filter(is_active=True)]
    expanded = []
    for word in words:
        alternatives = {word}
        for group in groups:
            if word in group:
                alternatives.update(group)
        expanded.append(sorted(alternatives))
    return expanded


def _text_query(expanded: list[list[str]]) -> SearchQuery | None:
    """AND across the user's words, OR across each word's synonyms; prefix-matched."""
    query = None
    for alternatives in expanded:
        part = None
        for term in alternatives:
            safe = re.sub(r"[^\w]", "", term)
            if not safe:
                continue
            q = SearchQuery(f"{safe}:*", search_type="raw", config="simple")
            part = q if part is None else part | q
        if part is not None:
            query = part if query is None else query & part
    return query


def search_products(query: str, qs: QuerySet[Product] | None = None, limit: int = 500) -> list[Product]:
    """Matching products, most relevant first (at most `limit`)."""
    qs = qs if qs is not None else public_products()
    query = (query or "").strip()[:MAX_QUERY_LENGTH]
    if not query:
        return []

    exact = product_by_code(query, qs)
    exact_ids = [exact.pk] if exact else []
    if query.isdigit():
        exact_ids += list(qs.filter(legacy_id=int(query)).values_list("pk", flat=True))

    vector = (
        SearchVector("name", weight="A", config="simple")
        + SearchVector("brand__name", "aliases__text", weight="B", config="simple")
        + SearchVector("category__name", "category__parent__name", weight="C", config="simple")
    )
    text_query = _text_query(expand_terms(query))
    try:
        with transaction.atomic():
            matched_text = Q(pk__in=[])
            rank = Value(0.0, output_field=FloatField())
            if text_query is not None:
                text_ids = qs.annotate(document=vector).filter(document=text_query).values("pk")
                matched_text = Q(pk__in=text_ids)
                rank = SearchRank(vector, text_query)
            matches = (
                qs.annotate(similarity=TrigramWordSimilarity(query, "name"))
                .filter(Q(pk__in=exact_ids) | matched_text | Q(similarity__gte=TRIGRAM_THRESHOLD))
                .annotate(
                    # exact code (10) > full-text word match (1 + rank) > fuzzy-only (similarity/2)
                    relevance=Case(When(pk__in=exact_ids, then=Value(10.0)), default=Value(0.0))
                    + Case(When(matched_text, then=Value(1.0) + Coalesce(rank, Value(0.0))), default=Value(0.0))
                    + F("similarity") / 2,
                )
            )
            ids = list(dict.fromkeys(matches.order_by("-relevance", "name").values_list("pk", flat=True)))[:limit]
    except DatabaseError:
        logger.exception("Full-text search failed; falling back to simple name match")
        return list(qs.filter(Q(name__icontains=query) | Q(sku=normalise_sku(query))).order_by("name")[:limit])
    order = {pk: i for i, pk in enumerate(ids)}
    results = list(qs.filter(pk__in=ids))
    results.sort(key=lambda p: order[p.pk])
    return results
