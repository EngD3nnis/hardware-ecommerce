from unittest import mock

import pytest
from django.db import DatabaseError
from rest_framework.test import APIClient

from apps.catalog import services as catalog
from apps.catalog.models import IdentifierKind
from apps.catalog.tests.factories import BrandFactory, CategoryFactory, ProductFactory
from apps.search.models import Synonym
from apps.search.services import search_products

pytestmark = pytest.mark.django_db


@pytest.fixture
def products(system_actor):
    locks = CategoryFactory(name="Locks")
    pipes = CategoryFactory(name="Pipes & Fittings")
    yale = BrandFactory(name="Yale")
    items = {
        "padlock": ProductFactory(
            sku="DWX-0648", legacy_id=648, name="Yale Bronze Padlock 50mm", category=locks, brand=yale
        ),
        "lockset": ProductFactory(sku="DWX-0700", name="Cylinder Lockset Chrome", category=locks),
        "ppr": ProductFactory(sku="DWX-1261", name="Green PPR Female Tee Elbow", category=pipes),
        "gi": ProductFactory(sku="DWX-0411", name="Galvanized 90 Degree Elbow", category=pipes),
        "hose": ProductFactory(sku="DWX-0421", name="Clear Garden Hose Pipe", category=pipes, barcode="6161100000017"),
        "draft": ProductFactory(sku="DWX-0999", name="Hidden Padlock", category=locks, status="DRAFT"),
    }
    catalog.add_identifier(items["lockset"], IdentifierKind.LEGACY_SKU, "OLD-LS-1", system_actor)
    catalog.add_alias(items["padlock"], "kufuli ya shaba", system_actor)
    return items


def skus(query):
    return [p.sku for p in search_products(query)]


@pytest.mark.parametrize(
    ("query", "first"),
    [
        ("DWX-0648", "DWX-0648"),  # exact SKU
        ("dwx-0648", "DWX-0648"),
        ("OLD-LS-1", "DWX-0700"),  # alternative SKU
        ("6161100000017", "DWX-0421"),  # barcode
        ("648", "DWX-0648"),  # old website id
        ("padlock", "DWX-0648"),  # word
        ("padlok", "DWX-0648"),  # typo
        ("kufuli", "DWX-0648"),  # alias
        ("yale", "DWX-0648"),  # brand
    ],
)
def test_finds_the_right_product_first(products, query, first):
    assert skus(query)[0] == first


def test_synonyms_expand(products):
    Synonym.objects.create(terms="galvanised, galvanized, gi")
    assert "DWX-0411" in skus("gi elbow")
    assert "DWX-0411" in skus("galvanised")


def test_all_words_must_match_for_full_text(products):
    result = skus("green elbow")
    assert result[0] == "DWX-1261"


def test_category_words_match(products):
    assert set(skus("fittings")) >= {"DWX-1261", "DWX-0411", "DWX-0421"}


def test_drafts_never_appear(products):
    assert "DWX-0999" not in skus("padlock")


def test_empty_and_nonsense(products):
    assert skus("") == [] and skus("   ") == [] and skus("zzqqxx") == []


def test_query_is_injection_safe(products):
    for query in ["'; drop table catalog_product; --", "a & | ! ( ) :*", "\\x00", "a" * 500]:
        search_products(query)  # must not raise
    assert skus("padlock")


def test_falls_back_when_full_text_fails(products):
    with mock.patch("apps.search.services.TrigramWordSimilarity", side_effect=DatabaseError("pg_trgm missing")):
        assert skus("Padlock") == ["DWX-0648"]


def test_api_uses_ranked_search(products):
    body = APIClient().get("/api/v1/catalog/products/?q=padlok").json()
    assert body["results"][0]["sku"] == "DWX-0648"
    body = APIClient().get("/api/v1/catalog/products/?q=elbow&category=category-1").json()
    assert {p["sku"] for p in body["results"]} <= {"DWX-1261", "DWX-0411"}
