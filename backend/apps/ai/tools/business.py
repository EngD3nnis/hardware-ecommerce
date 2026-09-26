"""The tools agents may use. Each is a thin, typed adapter over a domain service or selector.

Tools never invent data: prices come from the pricing service ("on request"
when there is none), stock from the inventory ledger, products from the catalogue.
"""

from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, F, Q, Sum
from django.utils import timezone
from pydantic import BaseModel, Field

from apps.automation import services as automation
from apps.catalog import services as catalog
from apps.catalog.models import CatalogReviewItem, Product, ProductStatus, ReviewStatus
from apps.catalog.selectors import product_by_code
from apps.core.exceptions import NotFound, ValidationError
from apps.customers import services as customers
from apps.inventory import services as inventory
from apps.inventory.models import StockBalance
from apps.notifications import services as notifications
from apps.notifications.models import InboundMessage, MessageStatus, OutboundMessage
from apps.payments.models import EventStatus, InboundPaymentEvent, Payment, PaymentState
from apps.pricing import services as pricing
from apps.procurement import services as procurement
from apps.procurement.models import Supplier, SupplierProduct
from apps.sales import services as sales
from apps.sales.models import Order, OrderLine, OrderStatus, Quotation
from apps.search.services import search_products

from .registry import Tier, Tool, register

LOW_STOCK_WORDING = {"in_stock": "In stock", "low": "Only a few left", "out": "Out of stock"}


def _product(code: str) -> Product:
    product = product_by_code(code)
    if product is None or product.status == ProductStatus.ARCHIVED:
        raise NotFound(f"No product with code {code}.")
    return product


def _brief(p: Product) -> dict:
    return {
        "sku": p.sku,
        "name": p.name,
        "category": str(p.category),
        "unit": p.unit_of_measure,
        "brand": p.brand.name if p.brand else None,
    }


# --- Catalogue / sales (LOW) --------------------------------------------------------------------


class SearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=100, description="Words, SKU or barcode")
    limit: int = Field(default=8, ge=1, le=20)


def search_products_tool(ctx, data: SearchInput) -> dict:
    results = search_products(data.query, limit=data.limit)
    return {"results": [_brief(p) for p in results], "count": len(results)}


register(
    Tool(
        "search_products",
        "Search the Dewmix catalogue. Returns only real products.",
        Tier.LOW,
        SearchInput,
        search_products_tool,
    )
)


class CodeInput(BaseModel):
    sku: str = Field(min_length=1, max_length=64)


def get_product_tool(ctx, data: CodeInput) -> dict:
    p = _product(data.sku)
    return {
        **_brief(p),
        "description": p.description,
        "material": p.material,
        "finish": p.finish,
        "colour": p.colour,
        "size": p.size_label,
        "status": p.status,
        "attributes": {
            v.attribute.label: f"{v.value} {v.attribute.unit}".strip()
            for v in p.attribute_values.select_related("attribute")
        },
    }


register(
    Tool(
        "get_product",
        "Full details of one product by SKU. Only state specifications listed here.",
        Tier.LOW,
        CodeInput,
        get_product_tool,
    )
)


def check_availability_tool(ctx, data: CodeInput) -> dict:
    """Customers see a band, not exact stock counts."""
    p = _product(data.sku)
    a = inventory.availability(p)
    balance = StockBalance.objects.filter(product=p, reorder_point__isnull=False).first()
    if a.available <= 0:
        band = "out"
    elif balance and a.available <= balance.reorder_point:
        band = "low"
    else:
        band = "in_stock"
    return {"sku": p.sku, "availability": band, "wording": LOW_STOCK_WORDING[band], "incoming_on_order": a.incoming > 0}


register(
    Tool(
        "check_availability",
        "Whether a product is in stock (in_stock / low / out). Never promise dates.",
        Tier.LOW,
        CodeInput,
        check_availability_tool,
    )
)


def get_price_tool(ctx, data: CodeInput) -> dict:
    quote = pricing.quote_price(_product(data.sku))
    return {
        "sku": data.sku.upper(),
        "price_on_request": quote.on_request,
        "amount": str(quote.amount) if quote.amount is not None else None,
        "currency": quote.currency,
        "includes_vat": quote.includes_vat,
        "instruction": "Say 'price on request' and create a quote draft." if quote.on_request else "",
    }


register(
    Tool(
        "get_price",
        "The official list price. If price_on_request, never guess a price.",
        Tier.LOW,
        CodeInput,
        get_price_tool,
    )
)


# --- Sales (MEDIUM) --------------------------------------------------------------------------------


class CustomerInput(BaseModel):
    phone: str = Field(min_length=6, max_length=30)
    name: str = Field(default="", max_length=150)


def get_or_create_customer_tool(ctx, data: CustomerInput) -> dict:
    customer, created = customers.get_or_create_by_phone(data.phone, ctx.actor, name=data.name)
    return {"customer_id": str(customer.pk), "name": customer.name, "created": created}


register(
    Tool(
        "get_or_create_customer",
        "Find the customer by phone number, creating a record if new.",
        Tier.MEDIUM,
        CustomerInput,
        get_or_create_customer_tool,
    )
)


class QuoteLine(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    quantity: Decimal = Field(gt=0, le=100000)


class QuoteDraftInput(BaseModel):
    customer_phone: str = Field(min_length=6, max_length=30)
    customer_name: str = Field(default="", max_length=150)
    lines: list[QuoteLine] = Field(min_length=1, max_length=30)
    notes: str = Field(default="", max_length=500)


def create_quote_draft_tool(ctx, data: QuoteDraftInput) -> dict:
    customer, _ = customers.get_or_create_by_phone(data.customer_phone, ctx.actor, name=data.customer_name)
    lines = [{"product": _product(line.sku), "quantity": line.quantity} for line in data.lines]
    quote = sales.create_quotation(
        ctx.actor, customer=customer, lines=lines, notes=data.notes, idempotency_key=f"agent:{ctx.run.pk}:{customer.pk}"
    )
    return {
        "quotation": quote.number,
        "status": quote.status,
        "lines": [
            {"sku": ln.sku, "unit_price": str(ln.unit_price) if ln.unit_price is not None else "on request"}
            for ln in quote.lines.all()
        ],
        "next_step": "Staff will review, price any 'on request' lines and send the quotation.",
    }


register(
    Tool(
        "create_quote_draft",
        "Create a DRAFT quotation for staff to review. Prices come from the system.",
        Tier.MEDIUM,
        QuoteDraftInput,
        create_quote_draft_tool,
    )
)


class ReplyInput(BaseModel):
    inbound_message_id: int
    text: str = Field(min_length=1, max_length=1500)


def reply_to_customer_tool(ctx, data: ReplyInput) -> dict:
    """Reply on WhatsApp to a message the customer sent in the last 24 hours (WhatsApp's service window)."""
    inbound = InboundMessage.objects.filter(pk=data.inbound_message_id).first()
    if inbound is None:
        raise NotFound("No such inbound message.")
    if inbound.received_at < timezone.now() - timedelta(hours=24):
        raise ValidationError("That message is older than 24 hours; escalate to staff instead.")
    customer, _ = customers.get_or_create_by_phone(inbound.from_phone, ctx.actor)
    message = notifications.queue(
        customer=customer,
        channel="WHATSAPP",
        template="agent_reply",
        body=data.text,
        actor=ctx.actor,
        idempotency_key=f"agent-reply:{ctx.run.pk}:{inbound.pk}",
        customer_initiated=True,
    )
    InboundMessage.objects.filter(pk=inbound.pk).update(handled=True, handled_by=ctx.actor.label)
    return {"queued": message is not None}


register(
    Tool(
        "reply_to_customer",
        "Send a WhatsApp reply to a customer's recent message.",
        Tier.MEDIUM,
        ReplyInput,
        reply_to_customer_tool,
    )
)


class EscalateInput(BaseModel):
    summary: str = Field(min_length=5, max_length=200)
    details: str = Field(default="", max_length=2000)
    inbound_message_id: int | None = None


def escalate_tool(ctx, data: EscalateInput) -> dict:
    task = automation.create_task(title=data.summary, description=data.details, created_by=ctx.actor)
    if data.inbound_message_id:
        InboundMessage.objects.filter(pk=data.inbound_message_id).update(handled=False)
    return {"task": str(task.pk), "message": "Staff have been asked to follow up."}


register(
    Tool(
        "escalate_to_human",
        "Hand the conversation or problem to staff when unsure or not allowed.",
        Tier.MEDIUM,
        EscalateInput,
        escalate_tool,
    )
)


# --- Inventory & procurement ----------------------------------------------------------------------------


class Empty(BaseModel):
    pass


def low_stock_tool(ctx, data: Empty) -> dict:
    items = [
        {
            "sku": b.product.sku,
            "name": b.product.name,
            "location": b.location.code,
            "available": str(b.available),
            "reorder_point": str(b.reorder_point),
            "reorder_quantity": str(b.reorder_quantity or ""),
        }
        for b in inventory.low_stock()[:100]
    ]
    return {"low_stock": items, "count": len(items)}


register(Tool("get_low_stock", "Products at or below their reorder point.", Tier.LOW, Empty, low_stock_tool))


class VelocityInput(BaseModel):
    days: int = Field(default=30, ge=7, le=365)
    limit: int = Field(default=30, ge=1, le=100)


def sales_velocity_tool(ctx, data: VelocityInput) -> dict:
    since = timezone.now() - timedelta(days=data.days)
    rows = (
        OrderLine.objects.filter(order__status=OrderStatus.DELIVERED, order__delivered_at__gte=since)
        .values("sku", "name")
        .annotate(qty=Sum("quantity"), orders=Count("order", distinct=True))
        .order_by("-qty")[: data.limit]
    )
    return {"days": data.days, "top_sellers": [{**r, "qty": str(r["qty"])} for r in rows]}


register(
    Tool(
        "get_sales_velocity",
        "Quantities sold per product over recent days (delivered orders).",
        Tier.LOW,
        VelocityInput,
        sales_velocity_tool,
    )
)


def dead_stock_tool(ctx, data: VelocityInput) -> dict:
    since = timezone.now() - timedelta(days=data.days)
    sold = OrderLine.objects.filter(order__delivered_at__gte=since).values("product_id")
    rows = (
        StockBalance.objects.filter(on_hand__gt=0)
        .exclude(product_id__in=sold)
        .select_related("product")
        .order_by("-on_hand")[: data.limit]
    )
    return {
        "days": data.days,
        "not_sold": [{"sku": b.product.sku, "name": b.product.name, "on_hand": str(b.on_hand)} for b in rows],
    }


register(
    Tool("get_dead_stock", "Products with stock but no sales in the period.", Tier.LOW, VelocityInput, dead_stock_tool)
)


class AlertInput(BaseModel):
    title: str = Field(min_length=5, max_length=200)
    details: str = Field(default="", max_length=2000)
    severity: str = Field(default="WARNING", pattern="^(INFO|WARNING|CRITICAL)$")
    key: str = Field(min_length=1, max_length=100, description="Stable identifier to avoid duplicate alerts")


def create_alert_tool(ctx, data: AlertInput) -> dict:
    incident = automation.raise_incident(
        kind=f"agent:{ctx.agent}",
        title=data.title,
        severity=data.severity,
        fingerprint=f"agent:{ctx.agent}:{data.key}",
        source=ctx.agent,
        details={"details": data.details},
    )
    return {"incident": str(incident.pk), "occurrences": incident.occurrences}


register(
    Tool(
        "create_alert",
        "Raise an alert (incident) for staff. Repeats with the same key are merged.",
        Tier.MEDIUM,
        AlertInput,
        create_alert_tool,
    )
)


class PODraftInput(BaseModel):
    supplier_code: str = Field(min_length=1, max_length=30)
    lines: list[QuoteLine] = Field(min_length=1, max_length=50)
    reason: str = Field(min_length=5, max_length=1000)


def create_po_draft_tool(ctx, data: PODraftInput) -> dict:
    supplier = Supplier.objects.filter(code=data.supplier_code, is_active=True).first()
    if supplier is None:
        raise NotFound(f"No active supplier {data.supplier_code}.")
    lines = [{"product": _product(line.sku), "quantity": line.quantity} for line in data.lines]
    order = procurement.create_purchase_order(supplier, lines, ctx.actor, notes=f"Suggested by agent: {data.reason}")
    return {
        "purchase_order": order.number,
        "status": order.status,
        "total": str(order.total),
        "next_step": "A person must submit and approve it; nothing is ordered yet.",
    }


register(
    Tool(
        "create_purchase_order_draft",
        "Draft a purchase order (uses the supplier's last known costs). Never places an order.",
        Tier.MEDIUM,
        PODraftInput,
        create_po_draft_tool,
    )
)


def supplier_options_tool(ctx, data: CodeInput) -> dict:
    options = SupplierProduct.objects.filter(product=_product(data.sku), supplier__is_active=True).select_related(
        "supplier"
    )
    return {
        "suppliers": [
            {
                "supplier_code": o.supplier.code,
                "name": o.supplier.name,
                "last_cost": str(o.last_cost) if o.last_cost is not None else None,
                "lead_time_days": o.lead_time_days or o.supplier.lead_time_days,
                "preferred": o.is_preferred,
            }
            for o in options
        ]
    }


register(
    Tool(
        "get_supplier_options",
        "Suppliers known for a product, with last cost and lead time.",
        Tier.LOW,
        CodeInput,
        supplier_options_tool,
    )
)


# --- Catalogue data ------------------------------------------------------------------------------------------


class ReviewListInput(BaseModel):
    kind: str = Field(default="", max_length=30)
    limit: int = Field(default=20, ge=1, le=50)


def open_review_items_tool(ctx, data: ReviewListInput) -> dict:
    qs = CatalogReviewItem.objects.filter(status=ReviewStatus.OPEN)
    if data.kind:
        qs = qs.filter(kind=data.kind)
    return {
        "items": [
            {"id": str(i.pk), "kind": i.kind, "summary": i.summary, "skus": [p.sku for p in i.products.all()]}
            for i in qs[: data.limit]
        ]
    }


register(
    Tool(
        "get_catalogue_issues", "Open catalogue data-quality issues.", Tier.LOW, ReviewListInput, open_review_items_tool
    )
)


class ProposalInput(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    field: str = Field(description="name, description, brand, colour, finish, material, size_label, category…")
    value: str = Field(min_length=1, max_length=2000)
    reason: str = Field(min_length=5, max_length=1000)


def propose_change_tool(ctx, data: ProposalInput) -> dict:
    proposal, created = catalog.propose_change(
        _product(data.sku), data.field, data.value, ctx.actor, source=f"agent:{ctx.agent}", reason=data.reason
    )
    return {
        "proposal": str(proposal.pk),
        "created": created,
        "status": proposal.status,
        "note": "A person must approve it before anything changes.",
    }


register(
    Tool(
        "propose_product_change",
        "Suggest a catalogue correction for human approval (never edits directly).",
        Tier.MEDIUM,
        ProposalInput,
        propose_change_tool,
    )
)


# --- Operations ------------------------------------------------------------------------------------------------


def ops_snapshot_tool(ctx, data: Empty) -> dict:
    """Deterministic facts for the operations summary."""
    now = timezone.now()
    day = now - timedelta(days=1)
    stuck = Order.objects.filter(
        Q(status=OrderStatus.CONFIRMED, confirmed_at__lt=now - timedelta(days=2))
        | Q(
            status__in=[OrderStatus.ALLOCATED, OrderStatus.PICKING, OrderStatus.PACKED],
            updated_at__lt=now - timedelta(days=3),
        )
        | Q(status=OrderStatus.DISPATCHED, dispatched_at__lt=now - timedelta(days=3))
    )
    return {
        "orders_last_24h": Order.objects.filter(created_at__gte=day).count(),
        "revenue_paid_last_24h": str(
            Payment.objects.filter(state=PaymentState.COMPLETED, received_at__gte=day).aggregate(t=Sum("amount"))["t"]
            or 0
        ),
        "quotations_waiting_for_prices": Quotation.objects.filter(status="DRAFT").count(),
        "stuck_orders": [{"order": o.number, "status": o.status} for o in stuck[:20]],
        "failed_messages": OutboundMessage.objects.filter(status=MessageStatus.FAILED).count(),
        "failed_payment_events": InboundPaymentEvent.objects.filter(status=EventStatus.FAILED).count(),
        "pending_mpesa_older_than_1h": Payment.objects.filter(
            state=PaymentState.PENDING, created_at__lt=now - timedelta(hours=1)
        ).count(),
        "unanswered_whatsapp": InboundMessage.objects.filter(handled=False).count(),
        "low_stock_count": inventory.low_stock().count(),
        "open_catalogue_issues": CatalogReviewItem.objects.filter(status=ReviewStatus.OPEN).count(),
        "available_negative_check": StockBalance.objects.filter(reserved__gt=F("on_hand")).count(),
    }


register(
    Tool(
        "get_operations_snapshot",
        "Counts of orders, payments, failures and backlogs right now.",
        Tier.LOW,
        Empty,
        ops_snapshot_tool,
    )
)


class TaskInput(BaseModel):
    title: str = Field(min_length=5, max_length=200)
    description: str = Field(default="", max_length=2000)


def create_task_tool(ctx, data: TaskInput) -> dict:
    task = automation.create_task(title=data.title, description=data.description, created_by=ctx.actor)
    return {"task": str(task.pk)}


register(Tool("create_internal_task", "Create a to-do for staff.", Tier.MEDIUM, TaskInput, create_task_tool))


# --- HIGH: requests that need a person's approval -----------------------------------------------------------


class PriceChangeInput(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    reason: str = Field(min_length=5, max_length=1000)


register(
    Tool(
        "request_price_change",
        "Ask a person to change a product's list price.",
        Tier.HIGH,
        PriceChangeInput,
        approval_action="pricing.set_price",
        approval_summary=lambda d: f"Set price of {d.sku} to KES {d.amount}",
    )
)


class StockAdjustmentInput(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    kind: str = Field(pattern="^(FOUND|LOST|DAMAGE|DAMAGE_WRITE_OFF|RETURN|RETURN_DAMAGED)$")
    quantity: Decimal = Field(gt=0)
    note: str = Field(min_length=5, max_length=255)
    reason: str = Field(default="", max_length=1000)


register(
    Tool(
        "request_stock_adjustment",
        "Ask a person to adjust stock (e.g. suspected loss).",
        Tier.HIGH,
        StockAdjustmentInput,
        approval_action="inventory.adjustment",
        approval_summary=lambda d: f"{d.kind} {d.quantity} × {d.sku}",
    )
)


class CancelInput(BaseModel):
    order_number: str = Field(min_length=4, max_length=20)
    reason: str = Field(min_length=5, max_length=255)


register(
    Tool(
        "request_order_cancellation",
        "Ask a person to cancel an order.",
        Tier.HIGH,
        CancelInput,
        approval_action="sales.cancel_order",
        approval_summary=lambda d: f"Cancel order {d.order_number}",
    )
)
