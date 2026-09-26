"""Purchasing: create, approve, send, receive and cancel purchase orders.

Approval policy (settings, see base.py):
- PURCHASE_AUTO_APPROVE_LIMIT: a person with the approve permission who
  submits an order at or below this total gets it approved immediately.
  Everything else waits for approval. Default 0: every order needs approval.
- PURCHASE_APPROVER_MUST_DIFFER: the approver must not be the creator.
- AI agents can create and submit drafts, but can never approve, send or
  receive (ADR 0004).
"""

from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.audit import services as audit
from apps.catalog.models import Product, ProductStatus
from apps.core.actors import Actor
from apps.core.exceptions import Conflict, PermissionDenied, ValidationError
from apps.core.numbering import next_number
from apps.inventory import services as inventory
from apps.inventory.models import MovementReason, StockLocation

from .models import (
    GoodsReceipt,
    GoodsReceiptLine,
    POStatus,
    PurchaseOrder,
    PurchaseOrderLine,
    Supplier,
    SupplierProduct,
)

TRANSITIONS = {
    POStatus.DRAFT: {POStatus.PENDING_APPROVAL, POStatus.APPROVED, POStatus.CANCELLED},
    POStatus.PENDING_APPROVAL: {POStatus.APPROVED, POStatus.DRAFT, POStatus.CANCELLED},
    POStatus.APPROVED: {POStatus.SENT, POStatus.PARTIALLY_RECEIVED, POStatus.RECEIVED, POStatus.CANCELLED},
    POStatus.SENT: {POStatus.PARTIALLY_RECEIVED, POStatus.RECEIVED, POStatus.CANCELLED},
    POStatus.PARTIALLY_RECEIVED: {POStatus.PARTIALLY_RECEIVED, POStatus.RECEIVED},
    POStatus.RECEIVED: set(),
    POStatus.CANCELLED: set(),
}


def _transition(order: PurchaseOrder, to: str, actor: Actor, reason: str = "") -> None:
    if to not in TRANSITIONS[order.status]:
        raise Conflict(
            f"A purchase order that is {order.get_status_display().lower()} cannot become {POStatus(to).label.lower()}."
        )
    before = order.status
    order.status = to
    audit.record(actor, "procurement.po.status", order, before={"status": before}, after={"status": to}, reason=reason)


def _lock(order: PurchaseOrder) -> PurchaseOrder:
    return PurchaseOrder.objects.select_for_update().get(pk=order.pk)


def _require_human(actor: Actor, what: str) -> None:
    if actor.is_agent:
        raise PermissionDenied(f"AI agents cannot {what}; a person must do it.")


# --- Drafting -----------------------------------------------------------------------------------


@transaction.atomic
def create_purchase_order(
    supplier: Supplier,
    lines: list[dict],
    actor: Actor,
    *,
    location: StockLocation | None = None,
    expected_date=None,
    notes: str = "",
) -> PurchaseOrder:
    """lines: [{"product": Product, "quantity": ..., "unit_cost": ... (optional: supplier's last cost)}]"""
    if not supplier.is_active:
        raise ValidationError(f"Supplier {supplier} is inactive.")
    if not lines:
        raise ValidationError("A purchase order needs at least one line.")
    order = PurchaseOrder.objects.create(
        number=next_number("PO"),
        supplier=supplier,
        location=location or inventory.default_location(),
        expected_date=expected_date,
        notes=notes,
        created_by=actor.label,
        created_by_agent=actor.is_agent,
    )
    for line in lines:
        _add_line(order, line["product"], line["quantity"], line.get("unit_cost"))
    audit.record(actor, "procurement.po.created", order, after={"lines": len(lines), "total": order.total})
    return order


def parse_cost(raw) -> Decimal:
    """Unit cost: a non-negative amount with at most 2 decimals (0 allowed, e.g. free samples)."""
    try:
        cost = Decimal(str(raw).replace(",", "").strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError("Unit cost must be a number.", details={"unit_cost": str(raw)}) from exc
    if not cost.is_finite() or cost < 0 or cost != cost.quantize(Decimal("0.01")):
        raise ValidationError(
            "Unit cost must be zero or more, with at most 2 decimals.", details={"unit_cost": str(raw)}
        )
    return cost


def _add_line(order: PurchaseOrder, product: Product, quantity, unit_cost=None) -> PurchaseOrderLine:
    if product.status == ProductStatus.ARCHIVED:
        raise ValidationError(f"{product.sku} is archived.")
    qty = inventory.validate_quantity(product, quantity)
    if unit_cost in (None, ""):
        known = SupplierProduct.objects.filter(supplier=order.supplier, product=product).first()
        if known is None or known.last_cost is None:
            raise ValidationError(f"No cost known for {product.sku} from {order.supplier}; enter a unit cost.")
        cost = known.last_cost
    else:
        cost = parse_cost(unit_cost)
    if order.lines.filter(product=product).exists():
        raise ValidationError(f"{product.sku} is already on this order; change that line instead.")
    return PurchaseOrderLine.objects.create(order=order, product=product, quantity=qty, unit_cost=cost)


@transaction.atomic
def add_line(order: PurchaseOrder, product: Product, quantity, actor: Actor, unit_cost=None) -> PurchaseOrderLine:
    order = _lock(order)
    if order.status != POStatus.DRAFT:
        raise Conflict("Only draft purchase orders can be changed.")
    line = _add_line(order, product, quantity, unit_cost)
    audit.record(actor, "procurement.po.line_added", order, after={"sku": product.sku, "quantity": line.quantity})
    return line


# --- Approval -----------------------------------------------------------------------------------


def _can_approve(actor: Actor, user=None) -> bool:
    return not actor.is_agent and user is not None and user.has_perm("procurement.approve_purchaseorder")


@transaction.atomic
def submit(order: PurchaseOrder, actor: Actor, *, user=None) -> PurchaseOrder:
    """Send a draft for approval, or approve it at once if policy allows."""
    order = _lock(order)
    if not order.lines.exists():
        raise ValidationError("Add at least one line before submitting.")
    order.submitted_at = timezone.now()
    limit = Decimal(str(settings.PURCHASE_AUTO_APPROVE_LIMIT))
    if _can_approve(actor, user) and order.total <= limit and not settings.PURCHASE_APPROVER_MUST_DIFFER:
        _transition(order, POStatus.APPROVED, actor, reason=f"Within auto-approve limit ({limit})")
        order.approved_by, order.approved_at = actor.label, timezone.now()
    else:
        _transition(order, POStatus.PENDING_APPROVAL, actor)
    order.save()
    return order


@transaction.atomic
def approve(order: PurchaseOrder, actor: Actor, *, user) -> PurchaseOrder:
    _require_human(actor, "approve purchases")
    if not _can_approve(actor, user):
        raise PermissionDenied("You do not have permission to approve purchase orders.")
    order = _lock(order)
    if settings.PURCHASE_APPROVER_MUST_DIFFER and order.created_by == actor.label:
        raise PermissionDenied("Someone other than the creator must approve this order.")
    _transition(order, POStatus.APPROVED, actor)
    order.approved_by, order.approved_at = actor.label, timezone.now()
    order.save()
    return order


@transaction.atomic
def send_back_to_draft(order: PurchaseOrder, actor: Actor, reason: str) -> PurchaseOrder:
    order = _lock(order)
    _transition(order, POStatus.DRAFT, actor, reason=reason)
    order.save()
    return order


@transaction.atomic
def mark_sent(order: PurchaseOrder, actor: Actor) -> PurchaseOrder:
    _require_human(actor, "send purchase orders to suppliers")
    order = _lock(order)
    _transition(order, POStatus.SENT, actor)
    order.sent_at = timezone.now()
    order.save()
    return order


@transaction.atomic
def cancel(order: PurchaseOrder, actor: Actor, reason: str) -> PurchaseOrder:
    if not reason.strip():
        raise ValidationError("Give a reason for cancelling.")
    order = _lock(order)
    if order.receipts.exists():
        raise Conflict("Goods have already been received on this order; it cannot be cancelled.")
    _transition(order, POStatus.CANCELLED, actor, reason=reason)
    order.cancelled_at, order.cancel_reason = timezone.now(), reason[:255]
    order.save()
    return order


# --- Receiving ------------------------------------------------------------------------------------


@transaction.atomic
def receive(
    order: PurchaseOrder,
    quantities: dict[int, object],
    actor: Actor,
    *,
    note: str = "",
    idempotency_key: str | None = None,
) -> GoodsReceipt:
    """Record goods arriving. quantities: {order_line_id: quantity received now}."""
    _require_human(actor, "receive goods")
    order = _lock(order)
    if idempotency_key and (existing := GoodsReceipt.objects.filter(idempotency_key=idempotency_key).first()):
        return existing
    if order.status not in (POStatus.APPROVED, POStatus.SENT, POStatus.PARTIALLY_RECEIVED):
        raise Conflict(f"Cannot receive goods on an order that is {order.get_status_display().lower()}.")
    lines = {line.pk: line for line in order.lines.select_for_update().select_related("product")}
    wanted = {}
    for line_id, raw in quantities.items():
        if raw in (None, "", 0, "0"):
            continue
        line = lines.get(int(line_id))
        if line is None:
            raise ValidationError(f"Line {line_id} is not on {order.number}.")
        qty = inventory.validate_quantity(line.product, raw)
        if qty > line.outstanding:
            raise ValidationError(
                f"{line.product.sku}: receiving {qty} but only {line.outstanding} outstanding.",
                details={"sku": line.product.sku, "outstanding": str(line.outstanding)},
            )
        wanted[line.pk] = qty
    if not wanted:
        raise ValidationError("Enter at least one received quantity.")

    receipt = GoodsReceipt.objects.create(
        order=order, received_by=actor.label, note=note[:255], idempotency_key=idempotency_key
    )
    for line_id, qty in wanted.items():
        line = lines[line_id]
        GoodsReceiptLine.objects.create(receipt=receipt, order_line=line, quantity=qty)
        inventory.receive(
            line.product,
            qty,
            actor,
            location=order.location,
            reason=MovementReason.PURCHASE_RECEIVED,
            source=inventory.Source.of(receipt),
            unit_cost=line.unit_cost,
            note=order.number,
            idempotency_key=f"receipt:{receipt.pk}:{line.pk}",
        )
        line.received_quantity += qty
        line.save(update_fields=["received_quantity"])
        SupplierProduct.objects.update_or_create(
            supplier=order.supplier, product=line.product, defaults={"last_cost": line.unit_cost}
        )

    fully = all(line.outstanding == 0 for line in lines.values())
    _transition(order, POStatus.RECEIVED if fully else POStatus.PARTIALLY_RECEIVED, actor)
    if fully:
        order.received_at = timezone.now()
    order.save()
    return receipt
