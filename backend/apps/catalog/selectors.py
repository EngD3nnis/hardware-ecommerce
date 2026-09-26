"""Catalogue read queries."""

from django.db.models import Prefetch, Q, QuerySet

from .models import Category, Product, ProductAttributeValue, ProductMedia, ProductStatus, normalise_sku


def public_products() -> QuerySet[Product]:
    """Products customers may see, with what list/detail views need prefetched."""
    return (
        Product.objects.filter(status=ProductStatus.ACTIVE)
        .select_related("category", "category__parent", "brand")
        .prefetch_related(
            Prefetch(
                "media", queryset=ProductMedia.objects.select_related("asset").order_by("-is_primary", "sort_order")
            ),
            Prefetch("attribute_values", queryset=ProductAttributeValue.objects.select_related("attribute")),
        )
    )


def filter_products(qs: QuerySet[Product], *, category: str = "", brand: str = "", q: str = "") -> QuerySet[Product]:
    """Simple filters. Ranked full-text search is apps.search (Stage 5)."""
    if category:
        qs = qs.filter(Q(category__slug=category) | Q(category__parent__slug=category))
    if brand:
        qs = qs.filter(brand__slug=brand)
    if q:
        code = normalise_sku(q)
        qs = qs.filter(
            Q(sku=code)
            | Q(identifiers__value=code)
            | Q(barcode=q.strip())
            | Q(name__icontains=q)
            | Q(aliases__text__icontains=q)
        ).distinct()
    return qs


def product_by_code(code: str, qs: QuerySet[Product] | None = None) -> Product | None:
    """Resolve a SKU, alternative/legacy SKU or barcode to one product."""
    qs = qs if qs is not None else Product.objects.all()
    code_upper = normalise_sku(code)
    return (
        qs.filter(sku=code_upper).first()
        or qs.filter(identifiers__value=code_upper).first()
        or qs.filter(barcode=code.strip()).first()
    )


def products_by_legacy_ids(ids: list[int], qs: QuerySet[Product] | None = None) -> list[Product]:
    """In the order requested; unknown ids are skipped (old links may reference deleted items)."""
    qs = qs if qs is not None else Product.objects.all()
    found = {p.legacy_id: p for p in qs.filter(legacy_id__in=ids)}
    return [found[i] for i in ids if i in found]


def category_tree() -> QuerySet[Category]:
    """Active top-level categories with active children prefetched."""
    return Category.objects.filter(parent__isnull=True, is_active=True).prefetch_related(
        Prefetch("children", queryset=Category.objects.filter(is_active=True))
    )
