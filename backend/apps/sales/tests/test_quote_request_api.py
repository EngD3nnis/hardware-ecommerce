import pytest
from rest_framework.test import APIClient

from apps.catalog.tests.factories import ProductFactory
from apps.customers.models import Customer
from apps.sales.models import Quotation

pytestmark = pytest.mark.django_db
URL = "/api/v1/quote-requests/"


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def products():
    return ProductFactory(sku="DWX-0000", legacy_id=0), ProductFactory(sku="DWX-0012", legacy_id=12)


def post(api, body, key="basket-key-0001"):
    return api.post(URL, body, format="json", HTTP_IDEMPOTENCY_KEY=key)


def body(**over):
    data = {
        "name": "Kamau",
        "phone": "0712 000 111",
        "items": [{"legacy_id": 0, "quantity": 2}, {"sku": "dwx-0012", "quantity": 1}],
    }
    data.update(over)
    return data


def test_creates_customer_and_draft_quotation(api, products):
    response = post(api, body())
    assert response.status_code == 201, response.json()
    data = response.json()
    quote = Quotation.objects.get(number=data["quotation"])
    assert quote.channel == "WEBSITE" and quote.status == "DRAFT" and quote.lines.count() == 2
    assert quote.customer.phone == "254712000111"
    assert data["whatsapp_url"].startswith("https://wa.me/254787151516?text=")
    assert quote.number in data["whatsapp_url"].replace("%2D", "-")


def test_retry_with_same_key_does_not_duplicate(api, products):
    first = post(api, body()).json()
    second = post(api, body()).json()
    assert first["quotation"] == second["quotation"]
    assert Quotation.objects.count() == 1 and Customer.objects.count() == 1


def test_requires_idempotency_key(api, products):
    response = api.post(URL, body(), format="json")
    assert response.status_code == 400 and "Idempotency-Key" in response.json()["error"]["message"]


@pytest.mark.parametrize(
    "bad",
    [
        {"items": []},
        {"items": [{"quantity": 1}]},
        {"items": [{"sku": "NOPE", "quantity": 1}]},
        {"items": [{"sku": "DWX-0000", "quantity": 0}]},
        {"items": [{"sku": "DWX-0000", "quantity": "1.5"}]},  # pieces are whole numbers
        {"phone": "12"},
        {"name": ""},
    ],
)
def test_validation(api, products, bad):
    response = post(api, body(**bad))
    assert response.status_code == 400
    assert Quotation.objects.count() == 0


def test_draft_products_are_not_quotable(api):
    ProductFactory(sku="HID-1", status="DRAFT")
    response = post(api, body(items=[{"sku": "HID-1", "quantity": 1}]))
    assert response.status_code == 400 and response.json()["error"]["details"]["unavailable"] == ["HID-1"]


def test_rate_limited(api, products, monkeypatch):
    from rest_framework.throttling import ScopedRateThrottle

    monkeypatch.setitem(ScopedRateThrottle.THROTTLE_RATES, "quote_request", "2/hour")
    assert post(api, body(), key="k-aaaaaaaa1").status_code == 201
    assert post(api, body(), key="k-aaaaaaaa2").status_code == 201
    assert post(api, body(), key="k-aaaaaaaa3").status_code == 429
