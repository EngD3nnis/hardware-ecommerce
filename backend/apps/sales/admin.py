from django import forms
from django.contrib import admin, messages

from apps.core.actors import Actor
from apps.core.exceptions import DomainError
from apps.core.numbering import next_number

from . import services
from .models import Order, OrderEvent, OrderLine, OrderStatus, Quotation, QuotationLine, QuoteStatus


def actor(request) -> Actor:
    return Actor.for_user(request.user)


def run(model_admin, request, queryset, fn, done):
    for obj in queryset:
        try:
            fn(obj)
            model_admin.message_user(request, f"{obj}: {done}.")
        except DomainError as exc:
            model_admin.message_user(request, f"{obj}: {exc.message}", messages.ERROR)


class QuotationLineForm(forms.ModelForm):
    class Meta:
        model = QuotationLine
        fields = ("product", "quantity", "unit_price", "discount")


class QuotationLineInline(admin.TabularInline):
    model = QuotationLine
    form = QuotationLineForm
    extra = 1
    autocomplete_fields = ("product",)
    fields = ("product", "quantity", "unit_price", "discount", "line_total")
    readonly_fields = ("line_total",)

    def _draft(self, obj):
        return obj is None or obj.status == QuoteStatus.DRAFT

    def has_add_permission(self, request, obj=None):
        return self._draft(obj)

    def has_change_permission(self, request, obj=None):
        return self._draft(obj)

    def has_delete_permission(self, request, obj=None):
        return self._draft(obj)


@admin.register(Quotation)
class QuotationAdmin(admin.ModelAdmin):
    list_display = ("number", "customer", "status", "channel", "quote_total", "valid_until", "created_by", "created_at")
    list_filter = ("status", "channel", "created_by_agent")
    search_fields = ("number", "customer__name", "customer__phone", "lines__sku")
    autocomplete_fields = ("customer",)
    inlines = (QuotationLineInline,)
    readonly_fields = (
        "number",
        "status",
        "valid_until",
        "created_by",
        "created_by_agent",
        "sent_at",
        "decided_at",
        "quote_total",
        "linked_order",
    )
    fields = (
        "number",
        "customer",
        "channel",
        "status",
        "notes",
        "internal_notes",
        "quote_total",
        "valid_until",
        "linked_order",
        "created_by",
        "created_by_agent",
        "sent_at",
        "decided_at",
    )
    actions = ("send_quotes", "accept_quotes", "reject_quotes", "convert_quotes")

    @admin.display(description="Total")
    def quote_total(self, obj):
        if not obj or not obj.pk:
            return "—"
        suffix = "" if obj.is_fully_priced else " (some lines not priced)"
        return f"KES {obj.total:,.2f}{suffix}"

    @admin.display(description="Send via WhatsApp")
    def whatsapp_link(self, obj):
        from django.utils.html import format_html

        from apps.notifications.services import render, whatsapp_click_to_chat

        if not obj or obj.status != QuoteStatus.SENT or not (obj.customer and obj.customer.phone):
            return "Available once sent, for customers with a phone number."
        url = whatsapp_click_to_chat(obj.customer.phone, render("quote_sent", obj))
        return format_html('<a class="button" href="{}" target="_blank" rel="noopener">Open WhatsApp</a>', url)

    @admin.display(description="Order")
    def linked_order(self, obj):
        return getattr(obj, "order", None) or "—"

    def save_model(self, request, obj, form, change):
        if not change:
            obj.number, obj.created_by = next_number("Q"), actor(request).label
        super().save_model(request, obj, form, change)

    def save_formset(self, request, form, formset, change):
        """New lines take a snapshot + list price via the service; price edits go through price_quote_line."""
        who, quote = actor(request), form.instance
        for line_form in formset.forms:
            if not line_form.has_changed() or not line_form.cleaned_data:
                continue
            data = line_form.cleaned_data
            try:
                if line_form in formset.deleted_forms:
                    if line_form.instance.pk:
                        services.remove_quote_line(line_form.instance, who)
                    continue
                line = line_form.instance if line_form.instance.pk else None
                if line is None:
                    line = services.add_quote_line(quote, data["product"], data["quantity"], who)
                elif "quantity" in line_form.changed_data:
                    services.remove_quote_line(line, who)
                    line = services.add_quote_line(quote, data["product"], data["quantity"], who)
                if data.get("unit_price") is not None and (
                    {"unit_price", "discount"} & set(line_form.changed_data) or line.unit_price is None
                ):
                    services.price_quote_line(
                        line, who, unit_price=data["unit_price"], discount=data.get("discount") or 0
                    )
            except DomainError as exc:
                self.message_user(request, exc.message, messages.ERROR)
        formset.new_objects, formset.changed_objects, formset.deleted_objects = [], [], []

    @admin.action(description="Mark as sent to customer", permissions=["change"])
    def send_quotes(self, request, queryset):
        run(self, request, queryset, lambda q: services.send_quotation(q, actor(request)), "sent")

    @admin.action(description="Customer accepted", permissions=["change"])
    def accept_quotes(self, request, queryset):
        run(self, request, queryset, lambda q: services.accept_quotation(q, actor(request)), "accepted")

    @admin.action(description="Customer rejected", permissions=["change"])
    def reject_quotes(self, request, queryset):
        run(self, request, queryset, lambda q: services.reject_quotation(q, actor(request), "In admin"), "rejected")

    @admin.action(description="Convert accepted quotation to order", permissions=["change"])
    def convert_quotes(self, request, queryset):
        run(self, request, queryset, lambda q: services.convert_to_order(q, actor(request)), "converted")


class OrderLineInline(admin.TabularInline):
    model = OrderLine
    extra = 0
    can_delete = False
    fields = ("sku", "name", "quantity", "unit_price", "discount", "line_total")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


class OrderEventInline(admin.TabularInline):
    model = OrderEvent
    extra = 0
    can_delete = False
    fields = ("at", "kind", "from_value", "to_value", "actor_label", "reason")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    """Orders are created from quotations (or via the API). Status changes only through actions."""

    list_display = ("number", "customer", "status", "payment_status", "total", "balance", "channel", "created_at")
    list_filter = ("status", "payment_status", "channel", "delivery_method")
    search_fields = ("number", "customer__name", "customer__phone", "lines__sku")
    inlines = (OrderLineInline, OrderEventInline)
    actions = ("allocate_orders", "start_picking", "cancel_orders")
    readonly_fields = [f.name for f in Order._meta.fields if f.name not in ("notes", "delivery_address")] + ["balance"]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Balance due")
    def balance(self, obj):
        return f"{obj.balance_due:,.2f}"

    def has_cancel_permission(self, request):
        return request.user.has_perm("sales.cancel_order")

    @admin.action(description="Allocate stock (reserve)", permissions=["change"])
    def allocate_orders(self, request, queryset):
        run(
            self,
            request,
            queryset.filter(status=OrderStatus.CONFIRMED),
            lambda o: services.allocate_order(o, actor(request)),
            "stock reserved",
        )

    @admin.action(description="Start picking (creates the fulfilment)", permissions=["change"])
    def start_picking(self, request, queryset):
        from apps.fulfillment import services as fulfillment  # fulfillment depends on sales; import lazily

        run(self, request, queryset, lambda o: fulfillment.start_picking(o, actor(request)), "picking started")

    @admin.action(description="Cancel order (releases stock)", permissions=["cancel"])
    def cancel_orders(self, request, queryset):
        who = actor(request)
        run(
            self,
            request,
            queryset,
            lambda o: services.cancel_order(o, who, f"Cancelled in admin by {who.label}"),
            "cancelled",
        )
