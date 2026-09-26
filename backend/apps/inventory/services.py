"""InventoryService: the only code that changes stock.

Rules every function follows:
- Runs in one transaction and locks the affected StockBalance rows with
  SELECT … FOR UPDATE in a stable order (product id, location id), so
  concurrent operations serialise instead of racing, and cannot deadlock.
- Checks the business rule (e.g. enough *available* stock) after taking the
  lock, raising InsufficientStock rather than letting the DB constraint fire.
- Writes a StockMovement for every bucket change, with reason, source and actor.
- Optional `idempotency_key`: a repeated call with the same key returns the
  first result instead of moving stock twice (retried tasks, double clicks,
  duplicate webhooks).
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import F, Q, Sum
from django.utils import timezone

from apps.audit import services as audit
from apps.catalog.models import Product, ProductStatus, UnitOfMeasure
from apps.core.actors import Actor
from apps.core.exceptions import Conflict, InvariantViolation, ValidationError
from apps.core.request_context import get_correlation_id

from .models import (
    ZERO,
    AdjustmentKind,
    Bucket,
    CountStatus,
    MovementReason,
    Reservation,
    ReservationStatus,
    StockAdjustment,
    StockBalance,
    StockCount,
    StockLocation,
    StockMovement,
)

# Units that are counted in whole numbers. Metres, kilograms and litres may be fractional.
WHOLE_NUMBER_UNITS = {
    UnitOfMeasure.PIECE,
    UnitOfMeasure.PAIR,
    UnitOfMeasure.SET,
    UnitOfMeasure.LENGTH,
    UnitOfMeasure.ROLL,
    UnitOfMeasure.SHEET,
    UnitOfMeasure.BAG,
    UnitOfMeasure.TIN,
    UnitOfMeasure.BOX,
    UnitOfMeasure.PACKET,
}


class InsufficientStock(Conflict):
    code = "insufficient_stock"
    default_message = "Not enough stock available."


@dataclass(frozen=True)
class Source:
    """What caused a stock change, e.g. Source("orders.order", order.pk)."""

    type: str = ""
    id: str = ""

    @classmethod
    def of(cls, obj) -> "Source":
        return cls(obj._meta.label_lower, str(obj.pk))


NO_SOURCE = Source()


@dataclass(frozen=True)
class Availability:
    on_hand: Decimal
    reserved: Decimal
    damaged: Decimal
    incoming: Decimal

    @property
    def available(self) -> Decimal:
        return self.on_hand - self.reserved


# --- Helpers -----------------------------------------------------------------------------


def default_location() -> StockLocation:
    location = StockLocation.objects.filter(is_default=True, is_active=True).first()
    if location is None:
        raise Conflict("No default stock location is configured.")
    return location


def validate_quantity(product: Product, quantity) -> Decimal:
    try:
        qty = Decimal(str(quantity).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError("Quantity must be a number.", details={"quantity": str(quantity)}) from exc
    if not qty.is_finite() or qty <= 0:
        raise ValidationError("Quantity must be greater than zero.", details={"quantity": str(quantity)})
    if qty != qty.quantize(Decimal("0.001")):
        raise ValidationError("Quantity can have at most 3 decimal places.")
    if product.unit_of_measure in WHOLE_NUMBER_UNITS and qty != qty.to_integral_value():
        raise ValidationError(
            f"{product.sku} is sold per {product.get_unit_of_measure_display().lower()}; use a whole number.",
            details={"quantity": str(quantity)},
        )
    return qty


def _lock_balances(*pairs: tuple[Product, StockLocation]) -> dict[tuple, StockBalance]:
    """Create-if-missing and lock balances in a stable order (prevents deadlocks)."""
    locked = {}
    for product, location in sorted(set(pairs), key=lambda p: (str(p[0].pk), str(p[1].pk))):
        balance, _ = StockBalance.objects.get_or_create(product=product, location=location)
        locked[(product.pk, location.pk)] = StockBalance.objects.select_for_update().get(pk=balance.pk)
    return locked


def _lock_one(product: Product, location: StockLocation) -> StockBalance:
    return _lock_balances((product, location))[(product.pk, location.pk)]


def _already_done(idempotency_key: str | None) -> StockMovement | None:
    if not idempotency_key:
        return None
    return StockMovement.objects.filter(idempotency_key=idempotency_key).first()


def _move(
    balance: StockBalance,
    bucket: str,
    delta: Decimal,
    reason: str,
    actor: Actor,
    *,
    source: Source = NO_SOURCE,
    note: str = "",
    unit_cost=None,
    idempotency_key: str | None = None,
) -> StockMovement:
    """Apply `delta` to one bucket of a locked balance and record the movement."""
    field = "on_hand" if bucket == Bucket.ON_HAND else "damaged"
    new_value = getattr(balance, field) + delta
    if new_value < 0:
        raise InsufficientStock(
            f"Not enough {field.replace('_', ' ')} stock of {balance.product.sku}.",
            details={"sku": balance.product.sku, field: str(getattr(balance, field)), "requested": str(-delta)},
        )
    if field == "on_hand" and balance.reserved > new_value:
        raise InsufficientStock(
            f"{balance.product.sku}: that stock is reserved for orders.",
            details={"sku": balance.product.sku, "available": str(balance.available), "requested": str(-delta)},
        )
    setattr(balance, field, new_value)
    balance.save(update_fields=[field, "updated_at"])
    return StockMovement.objects.create(
        product=balance.product,
        location=balance.location,
        bucket=bucket,
        quantity=delta,
        balance_after=new_value,
        reason=reason,
        unit_cost=unit_cost,
        source_type=source.type,
        source_id=source.id,
        note=note[:255],
        actor_type=actor.type,
        actor_label=actor.label[:255],
        correlation_id=get_correlation_id() or "",
        idempotency_key=idempotency_key,
    )


# --- Stock in / out ------------------------------------------------------------------------


@transaction.atomic
def receive(
    product: Product,
    quantity,
    actor: Actor,
    *,
    location: StockLocation | None = None,
    reason: str = MovementReason.PURCHASE_RECEIVED,
    source: Source = NO_SOURCE,
    unit_cost=None,
    note: str = "",
    idempotency_key: str | None = None,
) -> StockMovement:
    """Add sellable stock (purchase receipts, opening balances, found stock, returns)."""
    if reason not in (
        MovementReason.PURCHASE_RECEIVED,
        MovementReason.OPENING_BALANCE,
        MovementReason.RETURN,
        MovementReason.MANUAL_ADJUSTMENT,
    ):
        raise ValidationError(f"{reason} is not a receiving reason.")
    qty = validate_quantity(product, quantity)
    location = location or default_location()
    balance = _lock_one(product, location)
    if done := _already_done(idempotency_key):
        return done
    return _move(
        balance,
        Bucket.ON_HAND,
        qty,
        reason,
        actor,
        source=source,
        unit_cost=unit_cost,
        note=note,
        idempotency_key=idempotency_key,
    )


@transaction.atomic
def remove(
    product: Product,
    quantity,
    actor: Actor,
    *,
    location: StockLocation | None = None,
    reason: str = MovementReason.MANUAL_ADJUSTMENT,
    source: Source = NO_SOURCE,
    note: str = "",
    idempotency_key: str | None = None,
) -> StockMovement:
    """Take available stock out: counter sales without a reservation, lost stock."""
    if reason not in (MovementReason.SALE, MovementReason.MANUAL_ADJUSTMENT):
        raise ValidationError(f"{reason} is not a removal reason.")
    qty = validate_quantity(product, quantity)
    location = location or default_location()
    balance = _lock_one(product, location)
    if done := _already_done(idempotency_key):
        return done
    return _move(
        balance, Bucket.ON_HAND, -qty, reason, actor, source=source, note=note, idempotency_key=idempotency_key
    )


@transaction.atomic
def mark_damaged(
    product: Product,
    quantity,
    actor: Actor,
    *,
    location: StockLocation | None = None,
    note: str = "",
    source: Source = NO_SOURCE,
    idempotency_key: str | None = None,
) -> tuple[StockMovement, StockMovement]:
    qty = validate_quantity(product, quantity)
    location = location or default_location()
    balance = _lock_one(product, location)
    key = idempotency_key
    if done := _already_done(f"{key}:out" if key else None):
        return done, StockMovement.objects.get(idempotency_key=f"{key}:in")
    out = _move(
        balance,
        Bucket.ON_HAND,
        -qty,
        MovementReason.DAMAGE,
        actor,
        source=source,
        note=note,
        idempotency_key=f"{key}:out" if key else None,
    )
    into = _move(
        balance,
        Bucket.DAMAGED,
        qty,
        MovementReason.DAMAGE,
        actor,
        source=source,
        note=note,
        idempotency_key=f"{key}:in" if key else None,
    )
    return out, into


@transaction.atomic
def write_off_damaged(
    product: Product, quantity, actor: Actor, *, location=None, note: str = "", idempotency_key=None
) -> StockMovement:
    qty = validate_quantity(product, quantity)
    balance = _lock_one(product, location or default_location())
    if done := _already_done(idempotency_key):
        return done
    return _move(
        balance,
        Bucket.DAMAGED,
        -qty,
        MovementReason.DAMAGE_WRITE_OFF,
        actor,
        note=note,
        idempotency_key=idempotency_key,
    )


@transaction.atomic
def recover_damaged(
    product: Product, quantity, actor: Actor, *, location=None, note: str = "", idempotency_key=None
) -> StockMovement:
    """Damaged stock turned out to be sellable (e.g. repaired)."""
    qty = validate_quantity(product, quantity)
    balance = _lock_one(product, location or default_location())
    key = idempotency_key
    if done := _already_done(f"{key}:in" if key else None):
        return done
    _move(
        balance,
        Bucket.DAMAGED,
        -qty,
        MovementReason.DAMAGE_RECOVERED,
        actor,
        note=note,
        idempotency_key=f"{key}:out" if key else None,
    )
    return _move(
        balance,
        Bucket.ON_HAND,
        qty,
        MovementReason.DAMAGE_RECOVERED,
        actor,
        note=note,
        idempotency_key=f"{key}:in" if key else None,
    )


@transaction.atomic
def return_to_stock(
    product: Product,
    quantity,
    actor: Actor,
    *,
    sellable: bool = True,
    location=None,
    source: Source = NO_SOURCE,
    note: str = "",
    idempotency_key=None,
) -> StockMovement:
    qty = validate_quantity(product, quantity)
    balance = _lock_one(product, location or default_location())
    if done := _already_done(idempotency_key):
        return done
    bucket = Bucket.ON_HAND if sellable else Bucket.DAMAGED
    return _move(
        balance, bucket, qty, MovementReason.RETURN, actor, source=source, note=note, idempotency_key=idempotency_key
    )


@transaction.atomic
def transfer(
    product: Product,
    quantity,
    actor: Actor,
    *,
    from_location: StockLocation,
    to_location: StockLocation,
    note: str = "",
    idempotency_key=None,
) -> tuple[StockMovement, StockMovement]:
    if from_location.pk == to_location.pk:
        raise ValidationError("Choose two different locations.")
    qty = validate_quantity(product, quantity)
    balances = _lock_balances((product, from_location), (product, to_location))
    key = idempotency_key
    if done := _already_done(f"{key}:out" if key else None):
        return done, StockMovement.objects.get(idempotency_key=f"{key}:in")
    source = Source("inventory.transfer", key or "")
    out = _move(
        balances[(product.pk, from_location.pk)],
        Bucket.ON_HAND,
        -qty,
        MovementReason.TRANSFER_OUT,
        actor,
        source=source,
        note=note,
        idempotency_key=f"{key}:out" if key else None,
    )
    into = _move(
        balances[(product.pk, to_location.pk)],
        Bucket.ON_HAND,
        qty,
        MovementReason.TRANSFER_IN,
        actor,
        source=source,
        note=note,
        idempotency_key=f"{key}:in" if key else None,
    )
    return out, into


# --- Reservations ----------------------------------------------------------------------------


@transaction.atomic
def reserve(
    product: Product,
    quantity,
    actor: Actor,
    *,
    source: Source,
    location: StockLocation | None = None,
    expires_at=None,
    idempotency_key: str | None = None,
) -> Reservation:
    """Hold available stock for an order. Raises InsufficientStock if there isn't enough."""
    qty = validate_quantity(product, quantity)
    location = location or default_location()
    balance = _lock_one(product, location)
    if idempotency_key and (existing := Reservation.objects.filter(idempotency_key=idempotency_key).first()):
        return existing
    if balance.available < qty:
        raise InsufficientStock(
            f"Only {balance.available.normalize():f} of {product.sku} available.",
            details={"sku": product.sku, "available": str(balance.available), "requested": str(qty)},
        )
    balance.reserved += qty
    balance.save(update_fields=["reserved", "updated_at"])
    return Reservation.objects.create(
        product=product,
        location=location,
        quantity=qty,
        source_type=source.type,
        source_id=source.id,
        expires_at=expires_at,
        idempotency_key=idempotency_key,
    )


def _lock_reservation(reservation: Reservation) -> tuple[Reservation, StockBalance]:
    # Balance first, then the reservation: the same order everywhere.
    balance = _lock_one(reservation.product, reservation.location)
    return Reservation.objects.select_for_update().get(pk=reservation.pk), balance


@transaction.atomic
def release(reservation: Reservation, actor: Actor, reason: str = "") -> Reservation:
    """Give reserved stock back to available. Safe to call twice."""
    reservation, balance = _lock_reservation(reservation)
    if reservation.status != ReservationStatus.ACTIVE:
        return reservation
    if balance.reserved < reservation.quantity:
        raise InvariantViolation(
            "Reserved total is lower than an active reservation.",
            details={"sku": balance.product.sku, "reserved": str(balance.reserved)},
        )
    balance.reserved -= reservation.quantity
    balance.save(update_fields=["reserved", "updated_at"])
    reservation.status = ReservationStatus.RELEASED
    reservation.closed_at = timezone.now()
    reservation.close_reason = (reason or f"Released by {actor.label}")[:255]
    reservation.save(update_fields=["status", "closed_at", "close_reason", "updated_at"])
    return reservation


@transaction.atomic
def consume(reservation: Reservation, actor: Actor, *, source: Source | None = None, note: str = "") -> StockMovement:
    """The reserved goods left the shop: reserved and on_hand both drop, as a SALE movement."""
    reservation, balance = _lock_reservation(reservation)
    key = f"reservation:{reservation.pk}:consume"
    if reservation.status == ReservationStatus.CONSUMED:
        return StockMovement.objects.get(idempotency_key=key)
    if reservation.status != ReservationStatus.ACTIVE:
        raise Conflict("This reservation was released; reserve the stock again before dispatching.")
    balance.reserved -= reservation.quantity
    balance.save(update_fields=["reserved", "updated_at"])
    movement = _move(
        balance,
        Bucket.ON_HAND,
        -reservation.quantity,
        MovementReason.SALE,
        actor,
        source=source or Source(reservation.source_type, reservation.source_id),
        note=note,
        idempotency_key=key,
    )
    reservation.status = ReservationStatus.CONSUMED
    reservation.closed_at = timezone.now()
    reservation.save(update_fields=["status", "closed_at", "updated_at"])
    return movement


def release_expired(actor: Actor | None = None, now=None) -> int:
    """Release reservations past their expiry. Each in its own transaction."""
    actor = actor or Actor.system("reservation_expiry")
    now = now or timezone.now()
    released = 0
    expired = Reservation.objects.filter(status=ReservationStatus.ACTIVE, expires_at__lte=now)
    for reservation in expired.select_related("product", "location"):
        if release(reservation, actor, reason="Expired").status == ReservationStatus.RELEASED:
            released += 1
    return released


# --- Human overrides ---------------------------------------------------------------------------


def _apply_adjustment(a: StockAdjustment, who: Actor, key: str):
    common = {"location": a.location, "note": a.note, "idempotency_key": key}
    match a.kind:
        case AdjustmentKind.OPENING_BALANCE:
            return receive(
                a.product, a.quantity, who, reason=MovementReason.OPENING_BALANCE, source=Source.of(a), **common
            )
        case AdjustmentKind.FOUND:
            return receive(
                a.product, a.quantity, who, reason=MovementReason.MANUAL_ADJUSTMENT, source=Source.of(a), **common
            )
        case AdjustmentKind.LOST:
            return remove(a.product, a.quantity, who, source=Source.of(a), **common)
        case AdjustmentKind.DAMAGE:
            return mark_damaged(a.product, a.quantity, who, source=Source.of(a), **common)
        case AdjustmentKind.DAMAGE_WRITE_OFF:
            return write_off_damaged(a.product, a.quantity, who, **common)
        case AdjustmentKind.DAMAGE_RECOVERED:
            return recover_damaged(a.product, a.quantity, who, **common)
        case AdjustmentKind.RETURN:
            return return_to_stock(a.product, a.quantity, who, source=Source.of(a), **common)
        case AdjustmentKind.RETURN_DAMAGED:
            return return_to_stock(a.product, a.quantity, who, sellable=False, source=Source.of(a), **common)
    raise ValidationError(f"Unknown adjustment {a.kind!r}.")


@transaction.atomic
def record_adjustment(
    actor: Actor, *, product: Product, location: StockLocation, kind: str, quantity, note: str
) -> StockAdjustment:
    """A person's manual stock change. A note is mandatory."""
    if not (note or "").strip():
        raise ValidationError("Say why the stock is being adjusted.")
    if kind not in AdjustmentKind.values:
        raise ValidationError(f"Unknown adjustment {kind!r}.")
    qty = validate_quantity(product, quantity)
    adjustment = StockAdjustment.objects.create(
        product=product, location=location, kind=kind, quantity=qty, note=note.strip(), created_by=actor.label
    )
    _apply_adjustment(adjustment, actor, f"adjustment:{adjustment.pk}")
    audit.record(
        actor,
        "inventory.adjustment",
        product,
        after={"kind": kind, "quantity": qty, "location": location.code},
        reason=note,
    )
    return adjustment


@transaction.atomic
def start_count(location: StockLocation, actor: Actor, *, products=None, note: str = "") -> StockCount:
    """Open a stock count.

    products: None = products that have (or had) stock here; "all" = every
    active product (use for the first count); or an explicit list.
    """
    count = StockCount.objects.create(location=location, note=note, started_by=actor.label)
    if products is None:
        products = Product.objects.filter(stock_balances__location=location).distinct()
    elif products == "all":
        products = Product.objects.filter(status=ProductStatus.ACTIVE)
    count.lines.bulk_create([count.lines.model(count=count, product=p) for p in products])
    return count


@transaction.atomic
def load_counts(count: StockCount, rows: list[tuple[str, str]], actor: Actor) -> dict:
    """Fill counted quantities from (sku, quantity) pairs, e.g. a CSV export of a stock sheet.

    Adds lines for products not yet on the count. Returns counts of rows
    loaded, and lists of unknown SKUs and invalid quantities (nothing is guessed).
    """
    from apps.catalog.selectors import product_by_code

    count = StockCount.objects.select_for_update().get(pk=count.pk)
    if count.status != CountStatus.OPEN:
        raise Conflict("Counts can only be loaded into an open stock count.")
    result = {"loaded": 0, "unknown_skus": [], "invalid": []}
    for sku, raw in rows:
        product = product_by_code(sku)
        if product is None:
            result["unknown_skus"].append(sku)
            continue
        try:
            qty = Decimal(str(raw).replace(",", "").strip())
            if qty != 0:  # zero is a valid count; anything else must be a valid quantity
                validate_quantity(product, qty)
        except (InvalidOperation, ValueError, ValidationError):
            result["invalid"].append(f"{sku}: {raw!r}")
            continue
        count.lines.update_or_create(product=product, defaults={"counted_quantity": qty})
        result["loaded"] += 1
    audit.record(actor, "inventory.count.loaded", count, after={"loaded": result["loaded"]})
    return result


@transaction.atomic
def complete_count(count: StockCount, actor: Actor) -> StockCount:
    """Set on-hand stock to the counted quantities; differences become STOCKTAKE_CORRECTION movements."""
    if actor.is_agent:
        raise Conflict("Stock counts must be completed by a person.")
    count = StockCount.objects.select_for_update().get(pk=count.pk)
    if count.status != CountStatus.OPEN:
        raise Conflict(f"This count is already {count.get_status_display().lower()}.")
    lines = list(count.lines.filter(counted_quantity__isnull=False).select_related("product"))
    balances = _lock_balances(*[(line.product, count.location) for line in lines])
    problems = []
    for line in lines:
        balance = balances[(line.product.pk, count.location.pk)]
        if line.counted_quantity < balance.reserved:
            problems.append(f"{line.product.sku}: counted {line.counted_quantity}, but {balance.reserved} reserved")
        try:
            validate_quantity(line.product, line.counted_quantity or 1)
        except ValidationError as exc:
            problems.append(exc.message)
    if problems:
        raise Conflict("Resolve these before completing the count: " + "; ".join(problems))

    source = Source.of(count)
    for line in lines:
        balance = balances[(line.product.pk, count.location.pk)]
        line.system_quantity = balance.on_hand
        line.variance = line.counted_quantity - balance.on_hand
        if line.variance:
            _move(
                balance,
                Bucket.ON_HAND,
                line.variance,
                MovementReason.STOCKTAKE_CORRECTION,
                actor,
                source=source,
                note=f"Count {count.pk}",
                idempotency_key=f"count:{count.pk}:{line.product.pk}",
            )
        line.save(update_fields=["system_quantity", "variance"])
    count.status, count.completed_by, count.completed_at = CountStatus.COMPLETED, actor.label, timezone.now()
    count.save(update_fields=["status", "completed_by", "completed_at", "updated_at"])
    audit.record(
        actor,
        "inventory.count.completed",
        count,
        after={"lines": len(lines), "corrected": sum(1 for x in lines if x.variance)},
    )
    return count


# --- Checks ----------------------------------------------------------------------------------------


def reconcile() -> list[dict]:
    """Compare every balance with its ledger and active reservations. Never auto-corrects."""
    ledger = {
        (row["product_id"], row["location_id"], row["bucket"]): row["total"]
        for row in StockMovement.objects.values("product_id", "location_id", "bucket").annotate(total=Sum("quantity"))
    }
    held = {
        (row["product_id"], row["location_id"]): row["total"]
        for row in Reservation.objects.filter(status=ReservationStatus.ACTIVE)
        .values("product_id", "location_id")
        .annotate(total=Sum("quantity"))
    }
    mismatches = []
    for balance in StockBalance.objects.select_related("product", "location"):
        key = (balance.product_id, balance.location_id)
        expected = {
            "on_hand": ledger.get((*key, Bucket.ON_HAND), ZERO),
            "damaged": ledger.get((*key, Bucket.DAMAGED), ZERO),
            "reserved": held.get(key, ZERO),
        }
        for field, value in expected.items():
            if getattr(balance, field) != value:
                mismatches.append(
                    {
                        "sku": balance.product.sku,
                        "location": balance.location.code,
                        "field": field,
                        "balance": str(getattr(balance, field)),
                        "expected": str(value),
                    }
                )
    return mismatches


def availability(product: Product, location: StockLocation | None = None) -> Availability:
    balances = StockBalance.objects.filter(product=product)
    if location is not None:
        balances = balances.filter(location=location)
    totals = balances.aggregate(on_hand=Sum("on_hand"), reserved=Sum("reserved"), damaged=Sum("damaged"))
    from apps.procurement.selectors import incoming_quantity  # procurement depends on inventory, not vice versa

    return Availability(
        on_hand=totals["on_hand"] or ZERO,
        reserved=totals["reserved"] or ZERO,
        damaged=totals["damaged"] or ZERO,
        incoming=incoming_quantity(product, location),
    )


def low_stock(location: StockLocation | None = None):
    """Balances at or below their reorder point (available = on_hand − reserved)."""
    qs = StockBalance.objects.filter(reorder_point__isnull=False).filter(
        Q(on_hand__lte=F("reorder_point") + F("reserved"))
    )
    if location is not None:
        qs = qs.filter(location=location)
    return qs.select_related("product", "location")
