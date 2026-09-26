import pytest
from django.urls import reverse

from apps.catalog.tests.factories import ProductFactory
from apps.core.actors import Actor
from apps.inventory import services
from apps.inventory.models import StockAdjustment, StockBalance, StockLocation

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_client(client, superuser):
    client.force_login(superuser)
    return client


@pytest.mark.parametrize(
    "name",
    [
        "inventory_stocklocation",
        "inventory_stockbalance",
        "inventory_stockmovement",
        "inventory_reservation",
        "inventory_stockadjustment",
        "inventory_stockcount",
        "procurement_supplier",
        "procurement_purchaseorder",
    ],
)
def test_changelists_load(admin_client, name):
    services.receive(ProductFactory(), 3, Actor.system())
    assert admin_client.get(reverse(f"admin:{name}_changelist")).status_code == 200


def test_low_stock_filter(admin_client):
    product = ProductFactory()
    services.receive(product, 3, Actor.system())
    StockBalance.objects.update(reorder_point=5)
    response = admin_client.get(reverse("admin:inventory_stockbalance_changelist") + "?level=low")
    assert product.sku.encode() in response.content


def test_adjustment_through_admin_updates_stock(admin_client, superuser):
    product = ProductFactory()
    shop = StockLocation.objects.get(is_default=True)
    data = {"product": product.pk, "location": shop.pk, "kind": "OPENING_BALANCE", "quantity": "7", "note": "count"}
    assert admin_client.post(reverse("admin:inventory_stockadjustment_add"), data).status_code == 302
    assert StockBalance.objects.get(product=product).on_hand == 7
    assert StockAdjustment.objects.get().created_by == superuser.email


def test_adjustment_form_refuses_removing_missing_stock(admin_client):
    product = ProductFactory()
    shop = StockLocation.objects.get(is_default=True)
    data = {"product": product.pk, "location": shop.pk, "kind": "LOST", "quantity": "1", "note": "gone"}
    response = admin_client.post(reverse("admin:inventory_stockadjustment_add"), data)
    assert response.status_code == 200  # form error, nothing saved
    assert b"Only 0 available" in response.content
    assert not StockAdjustment.objects.exists()


def test_availability_api_is_staff_only(client, staff_user):
    from rest_framework.test import APIClient

    product = ProductFactory(sku="AV-1")
    services.receive(product, 4, Actor.system())
    api = APIClient()
    url = "/api/v1/inventory/products/av-1/availability/"
    assert api.get(url).status_code == 401
    api.force_authenticate(staff_user)
    body = api.get(url).json()
    assert (body["on_hand"], body["available"], body["incoming"]) == ("4.000", "4.000", "0")
    assert body["locations"][0]["location"] == "kenol"
