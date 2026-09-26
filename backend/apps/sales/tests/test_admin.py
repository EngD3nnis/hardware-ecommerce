import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "name",
    [
        "customers_customer",
        "sales_quotation",
        "sales_order",
        "payments_payment",
        "payments_refund",
        "payments_inboundpaymentevent",
    ],
)
def test_changelists_and_add_pages_load(client, superuser, name):
    client.force_login(superuser)
    assert client.get(reverse(f"admin:{name}_changelist")).status_code == 200
    if name not in ("sales_order", "payments_inboundpaymentevent"):
        assert client.get(reverse(f"admin:{name}_add")).status_code == 200
