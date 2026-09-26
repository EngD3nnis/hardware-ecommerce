"""Actions that may be requested (e.g. by an agent) and run only after a person approves.

Each receives the approved values and runs as the approving person, through
the same services the admin uses, so all normal checks and auditing apply.
"""

from apps.catalog.models import Product
from apps.catalog.selectors import product_by_code
from apps.core.exceptions import NotFound, PermissionDenied
from apps.inventory import services as inventory
from apps.inventory.models import StockLocation
from apps.pricing import services as pricing
from apps.sales import services as sales
from apps.sales.models import Order

from .services import register_action


def _product(sku: str) -> Product:
    product = product_by_code(sku)
    if product is None:
        raise NotFound(f"No product {sku}.")
    return product


@register_action("pricing.set_price")
def set_price(values: dict, actor, user) -> dict:
    if not user.has_perm("pricing.add_productprice") and not user.has_perm("catalog.change_product"):
        raise PermissionDenied("You may not change prices.")
    product = _product(values["sku"])
    price = pricing.set_price(product, values["amount"], actor, source="approval", note=values.get("reason", ""))
    return {"sku": product.sku, "amount": str(price.amount) if price else "unchanged"}


@register_action("inventory.adjustment")
def adjust_stock(values: dict, actor, user) -> dict:
    if not user.has_perm("inventory.add_stockadjustment"):
        raise PermissionDenied("You may not adjust stock.")
    location = StockLocation.objects.filter(code=values.get("location") or "").first() or inventory.default_location()
    adjustment = inventory.record_adjustment(
        actor,
        product=_product(values["sku"]),
        location=location,
        kind=values["kind"],
        quantity=values["quantity"],
        note=values["note"],
    )
    return {"adjustment": str(adjustment.pk)}


@register_action("sales.cancel_order")
def cancel_order(values: dict, actor, user) -> dict:
    if not user.has_perm("sales.cancel_order"):
        raise PermissionDenied("You may not cancel orders.")
    order = Order.objects.filter(number=values["order_number"]).first()
    if order is None:
        raise NotFound(f"No order {values['order_number']}.")
    sales.cancel_order(order, actor, values["reason"])
    return {"order": order.number, "status": "CANCELLED"}
