"""Price lookup and price changes. The only place prices are written."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.audit import services as audit
from apps.catalog.models import Product
from apps.core.actors import Actor
from apps.core.exceptions import Conflict, ValidationError

from .models import PriceList, ProductPrice

MAX_PRICE = Decimal("100000000")  # sanity bound; KES 100m for a single unit is a typo


@dataclass(frozen=True)
class PriceQuote:
    """What a salesperson (or agent) may say about a product's price."""

    amount: Decimal | None
    currency: str
    price_list: str
    on_request: bool
    includes_vat: bool

    @property
    def display(self) -> str:
        return "Price on request" if self.on_request else f"{self.currency} {self.amount:,.2f}"


def default_price_list() -> PriceList:
    price_list = PriceList.objects.filter(is_default=True, is_active=True).first()
    if price_list is None:
        raise Conflict("No default price list is configured.")
    return price_list


def current_price(product: Product, price_list: PriceList | None = None, at: datetime | None = None):
    price_list = price_list or default_price_list()
    at = at or timezone.now()
    return (
        ProductPrice.objects.filter(product=product, price_list=price_list, valid_from__lte=at)
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gt=at))
        .first()
    )


def quote_price(product: Product, price_list: PriceList | None = None, at: datetime | None = None) -> PriceQuote:
    price_list = price_list or default_price_list()
    price = None if product.price_on_request else current_price(product, price_list, at)
    return PriceQuote(
        amount=price.amount if price else None,
        currency=price_list.currency,
        price_list=price_list.code,
        on_request=price is None,
        includes_vat=price_list.prices_include_vat,
    )


def parse_amount(raw) -> Decimal:
    try:
        amount = Decimal(str(raw).replace(",", "").strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError("Price must be a number.", details={"price": str(raw)}) from exc
    if not amount.is_finite() or amount <= 0:
        raise ValidationError("Price must be greater than zero.", details={"price": str(raw)})
    if amount >= MAX_PRICE:
        raise ValidationError("Price is implausibly large; check for a typo.", details={"price": str(raw)})
    if amount != amount.quantize(Decimal("0.01")):
        raise ValidationError("Price can have at most 2 decimal places.", details={"price": str(raw)})
    return amount


@transaction.atomic
def set_price(
    product: Product,
    amount,
    actor: Actor,
    *,
    price_list: PriceList | None = None,
    effective_from: datetime | None = None,
    source: str = "",
    note: str = "",
) -> ProductPrice | None:
    """Make `amount` the price from `effective_from` (default now).

    Returns the new price, or None if the price was already `amount`.
    Back-dating before the latest existing price is refused: history is not rewritten.
    """
    amount = parse_amount(amount)
    price_list = price_list or default_price_list()
    effective_from = effective_from or timezone.now()

    # Serialise price changes for this product so two edits cannot interleave.
    Product.objects.select_for_update().filter(pk=product.pk).first()
    latest = ProductPrice.objects.filter(product=product, price_list=price_list).order_by("-valid_from").first()
    if latest and effective_from <= latest.valid_from:
        raise Conflict("A later price already exists; prices cannot be back-dated before it.")
    if latest and latest.valid_to is None and latest.amount == amount:
        return None
    if latest and latest.valid_to is None:
        latest.valid_to = effective_from
        latest.save(update_fields=["valid_to", "updated_at"])
    try:
        with transaction.atomic():
            price = ProductPrice.objects.create(
                product=product,
                price_list=price_list,
                amount=amount,
                valid_from=effective_from,
                source=source,
                created_by=actor.label,
                note=note,
            )
    except IntegrityError as exc:
        raise Conflict("This price overlaps an existing price period.") from exc
    audit.record(
        actor,
        "pricing.price.set",
        product,
        before={"amount": latest.amount if latest and latest.valid_to == effective_from else None},
        after={"amount": amount, "price_list": price_list.code, "valid_from": effective_from},
        reason=note,
    )
    return price


@transaction.atomic
def end_price(product: Product, actor: Actor, *, price_list: PriceList | None = None, note: str = "") -> bool:
    """Stop the current price; the product becomes "price on request". Returns False if none was open."""
    price_list = price_list or default_price_list()
    Product.objects.select_for_update().filter(pk=product.pk).first()
    open_price = ProductPrice.objects.filter(product=product, price_list=price_list, valid_to__isnull=True).first()
    if open_price is None:
        return False
    now = timezone.now()
    if open_price.valid_from >= now:
        open_price.delete()  # never took effect
    else:
        open_price.valid_to = now
        open_price.save(update_fields=["valid_to", "updated_at"])
    audit.record(actor, "pricing.price.ended", product, before={"amount": open_price.amount}, reason=note)
    return True
