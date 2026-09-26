import pytest
from rest_framework.test import APIClient

from apps.catalog import services
from apps.catalog.models import IdentifierKind, ProductStatus
from apps.core.models import BusinessProfile
from apps.pricing import services as pricing

from .factories import BrandFactory, CategoryFactory, ProductFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def catalogue(system_actor, image_bytes):
    locks = CategoryFactory(name="Locks", slug="locks")
    padlocks = CategoryFactory(name="Padlocks", slug="locks-padlocks", parent=locks)
    yale = BrandFactory(name="Yale", slug="yale")
    padlock = ProductFactory(sku="DWX-0007", legacy_id=7, name="Yale Padlock 50mm", category=padlocks, brand=yale)
    zero = ProductFactory(sku="DWX-0000", legacy_id=0, name="Aluminium Bathroom Set")
    ProductFactory(sku="DWX-0099", legacy_id=99, name="Hidden draft", status=ProductStatus.DRAFT)
    asset, _ = services.store_image(image_bytes(), "7.jpg")
    services.attach_image(padlock, asset, system_actor)
    services.add_identifier(padlock, IdentifierKind.LEGACY_SKU, "OLD-PL-50", system_actor)
    services.add_alias(padlock, "kufuli", system_actor)  # Swahili for padlock
    pricing.set_price(padlock, "850", system_actor)
    return {"padlock": padlock, "zero": zero}


def test_list_is_public_and_hides_drafts(api, catalogue):
    response = api.get("/api/v1/catalog/products/")
    assert response.status_code == 200
    skus = {p["sku"] for p in response.json()["results"]}
    assert skus == {"DWX-0007", "DWX-0000"}


def test_prices_hidden_unless_enabled(api, catalogue):
    body = api.get("/api/v1/catalog/products/DWX-0007/").json()
    assert body["price"] == {"on_request": True, "amount": None, "currency": "KES", "includes_vat": True}

    BusinessProfile.objects.update(show_prices_online=True)
    body = api.get("/api/v1/catalog/products/DWX-0007/").json()
    # price_on_request is still True (factory default) → the item is quoted individually
    assert body["price"]["on_request"] is True

    catalogue["padlock"].price_on_request = False
    catalogue["padlock"].save()
    body = api.get("/api/v1/catalog/products/DWX-0007/").json()
    assert body["price"] == {"on_request": False, "amount": "850.00", "currency": "KES", "includes_vat": True}


def test_detail_payload(api, catalogue):
    body = api.get("/api/v1/catalog/products/DWX-0007/").json()
    assert body["name"] == "Yale Padlock 50mm"
    assert body["category"] == {
        "slug": "locks-padlocks",
        "name": "Padlocks",
        "parent": {"slug": "locks", "name": "Locks"},
    }
    assert body["brand"] == {"slug": "yale", "name": "Yale"}
    assert len(body["images"]) == 1 and body["images"][0]["primary"] is True
    assert body["images"][0]["url"].startswith("http://testserver/media/products/")


@pytest.mark.parametrize("code", ["DWX-0007", "dwx-0007", "OLD-PL-50"])
def test_detail_resolves_sku_case_insensitively_and_identifiers(api, catalogue, code):
    assert api.get(f"/api/v1/catalog/products/{code}/").json()["sku"] == "DWX-0007"


def test_detail_404_for_drafts_and_unknown(api, catalogue):
    for code in ("DWX-0099", "NOPE"):
        response = api.get(f"/api/v1/catalog/products/{code}/")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("q=DWX-0007", {"DWX-0007"}),
        ("q=kufuli", {"DWX-0007"}),
        ("q=bathroom", {"DWX-0000"}),
        ("category=locks", {"DWX-0007"}),
        ("brand=yale", {"DWX-0007"}),
    ],
)
def test_filters(api, catalogue, query, expected):
    results = api.get(f"/api/v1/catalog/products/?{query}").json()["results"]
    assert {p["sku"] for p in results} == expected


def test_legacy_ids_resolve_in_order_including_zero(api, catalogue):
    response = api.get("/api/v1/catalog/products/legacy/?ids=7,0,99,12345,7")
    assert response.status_code == 200
    # 99 is a draft and 12345 does not exist; duplicates collapse; order preserved.
    assert [p["legacy_id"] for p in response.json()] == [7, 0]


@pytest.mark.parametrize("ids", ["", "1,a", "-1", ",".join(str(i) for i in range(101))])
def test_legacy_ids_validation(api, catalogue, ids):
    response = api.get(f"/api/v1/catalog/products/legacy/?ids={ids}")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


def test_categories_and_brands(api, catalogue):
    categories = api.get("/api/v1/catalog/categories/").json()
    locks = next(c for c in categories if c["slug"] == "locks")
    assert locks["children"] == [{"slug": "locks-padlocks", "name": "Padlocks"}]
    assert api.get("/api/v1/catalog/brands/").json() == [{"slug": "yale", "name": "Yale"}]


def test_business_profile_is_public(api):
    body = api.get("/api/v1/business-profile/").json()
    assert body["whatsapp_sales_number"] == "254787151516"
    assert body["show_prices_online"] is False


def test_public_api_is_rate_limited(api, catalogue, monkeypatch):
    from rest_framework.throttling import ScopedRateThrottle

    monkeypatch.setitem(ScopedRateThrottle.THROTTLE_RATES, "public", "2/min")
    assert api.get("/api/v1/catalog/categories/").status_code == 200
    assert api.get("/api/v1/catalog/categories/").status_code == 200
    assert api.get("/api/v1/catalog/categories/").status_code == 429


def test_openapi_schema_is_served(api):
    response = api.get("/api/v1/schema/")
    assert response.status_code == 200
    assert b"/api/v1/catalog/products/" in response.content


def test_list_query_count_is_bounded(api, catalogue, django_assert_max_num_queries):
    for n in range(20):
        ProductFactory(sku=f"BULK-{n}")
    with django_assert_max_num_queries(12):
        api.get("/api/v1/catalog/products/")
