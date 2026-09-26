import uuid

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html

from apps.core.actors import Actor
from apps.core.exceptions import DomainError
from apps.core.numbering import next_number
from apps.inventory.services import validate_quantity

from . import services
from .models import POStatus, PurchaseOrder, PurchaseOrderLine, Supplier, SupplierProduct


def actor(request) -> Actor:
    return Actor.for_user(request.user)


class SupplierProductInline(admin.TabularInline):
    model = SupplierProduct
    extra = 0
    autocomplete_fields = ("product",)
    fields = ("product", "supplier_sku", "last_cost", "lead_time_days", "min_order_quantity", "is_preferred")


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "contact_person", "phone", "payment_terms_days", "lead_time_days", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "code", "contact_person", "phone", "email")
    prepopulated_fields = {"code": ("name",)}
    inlines = (SupplierProductInline,)


class PurchaseOrderLineInline(admin.TabularInline):
    model = PurchaseOrderLine
    extra = 1
    autocomplete_fields = ("product",)
    fields = ("product", "quantity", "unit_cost", "received_quantity", "line_total")
    readonly_fields = ("received_quantity", "line_total")

    def _editable(self, obj):
        return obj is None or obj.status == POStatus.DRAFT

    def has_add_permission(self, request, obj=None):
        return self._editable(obj) and super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        return self._editable(obj) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self._editable(obj) and super().has_delete_permission(request, obj)

    @admin.display(description="Total")
    def line_total(self, obj):
        return obj.line_total if obj.pk else "—"


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(admin.ModelAdmin):
    list_display = ("number", "supplier", "status", "order_total", "expected_date", "created_by", "approved_by")
    list_filter = ("status", "supplier", "created_by_agent")
    search_fields = ("number", "supplier__name", "lines__product__sku")
    inlines = (PurchaseOrderLineInline,)
    actions = ("submit_orders", "approve_orders", "mark_orders_sent", "cancel_orders")
    readonly_fields = (
        "number", "status", "order_total", "receive_link", "created_by", "created_by_agent", "submitted_at",
        "approved_by", "approved_at", "sent_at", "received_at", "cancelled_at", "cancel_reason",
    )  # fmt: skip
    fieldsets = (
        (None, {"fields": ("number", "supplier", "location", "status", "order_total", "receive_link")}),
        ("Details", {"fields": ("expected_date", "notes")}),
        ("History", {"classes": ("collapse",), "fields": (
            "created_by", "created_by_agent", "submitted_at", "approved_by", "approved_at", "sent_at",
            "received_at", "cancelled_at", "cancel_reason")}),
    )  # fmt: skip

    def get_readonly_fields(self, request, obj=None):
        fields = list(self.readonly_fields)
        if obj is not None and obj.status != POStatus.DRAFT:
            fields += ["supplier", "location", "expected_date"]
        return fields

    @admin.display(description="Total (KES)")
    def order_total(self, obj):
        return f"{obj.total:,.2f}" if obj and obj.pk else "—"

    @admin.display(description="Receive")
    def receive_link(self, obj):
        if obj and obj.status in (POStatus.APPROVED, POStatus.SENT, POStatus.PARTIALLY_RECEIVED):
            url = reverse("admin:procurement_purchaseorder_receive", args=[obj.pk])
            return format_html('<a class="button" href="{}">Receive goods</a>', url)
        return "—"

    def save_model(self, request, obj, form, change):
        if not change:
            obj.number = next_number("PO")
            obj.created_by = actor(request).label
        super().save_model(request, obj, form, change)

    def save_formset(self, request, form, formset, change):
        # Lines are validated like the service does (whole units, known cost).
        instances = formset.save(commit=False)
        for obj in formset.deleted_objects:
            obj.delete()
        for line in instances:
            try:
                line.quantity = validate_quantity(line.product, line.quantity)
            except DomainError as exc:
                self.message_user(request, exc.message, messages.ERROR)
                continue
            line.save()
        formset.save_m2m()

    # --- Actions ------------------------------------------------------------------------

    def has_approve_permission(self, request):
        return request.user.has_perm("procurement.approve_purchaseorder")

    def _run(self, request, queryset, fn, done):
        for order in queryset:
            try:
                fn(order)
                self.message_user(request, f"{order.number}: {done}.")
            except DomainError as exc:
                self.message_user(request, f"{order.number}: {exc.message}", messages.ERROR)

    @admin.action(description="Submit for approval", permissions=["change"])
    def submit_orders(self, request, queryset):
        who = actor(request)
        self._run(request, queryset, lambda o: services.submit(o, who, user=request.user), "submitted")

    @admin.action(description="Approve", permissions=["approve"])
    def approve_orders(self, request, queryset):
        who = actor(request)
        self._run(request, queryset, lambda o: services.approve(o, who, user=request.user), "approved")

    @admin.action(description="Mark as sent to supplier", permissions=["change"])
    def mark_orders_sent(self, request, queryset):
        self._run(request, queryset, lambda o: services.mark_sent(o, actor(request)), "marked sent")

    @admin.action(description="Cancel (only if nothing received)", permissions=["change"])
    def cancel_orders(self, request, queryset):
        who = actor(request)
        self._run(
            request, queryset, lambda o: services.cancel(o, who, f"Cancelled in admin by {who.label}"), "cancelled"
        )

    # --- Receive goods page -------------------------------------------------------------

    def get_urls(self):
        return [
            path(
                "<uuid:pk>/receive/",
                self.admin_site.admin_view(self.receive_view),
                name="procurement_purchaseorder_receive",
            ),
            *super().get_urls(),
        ]

    def receive_view(self, request, pk):
        if not request.user.has_perm("inventory.add_stockadjustment") and not request.user.has_perm(
            "procurement.change_purchaseorder"
        ):
            raise DjangoPermissionDenied
        order = get_object_or_404(PurchaseOrder, pk=pk)
        error = ""
        if request.method == "POST":
            quantities = {
                key.removeprefix("line_"): value for key, value in request.POST.items() if key.startswith("line_")
            }
            try:
                services.receive(
                    order,
                    quantities,
                    actor(request),
                    note=request.POST.get("note", ""),
                    idempotency_key=f"admin-receive:{request.POST.get('idempotency_key', '')}",
                )
                self.message_user(request, f"Goods received on {order.number}; stock updated.")
                return redirect("admin:procurement_purchaseorder_change", order.pk)
            except DomainError as exc:
                error = exc.message
        context = {
            **self.admin_site.each_context(request),
            "title": f"Receive goods: {order.number}",
            "order": order,
            "lines": order.lines.select_related("product"),
            "error": error,
            # A new key per form load: double-submitting the same form records one receipt.
            "idempotency_key": uuid.uuid4().hex,
            "opts": self.model._meta,
        }
        return TemplateResponse(request, "admin/procurement/receive.html", context)
