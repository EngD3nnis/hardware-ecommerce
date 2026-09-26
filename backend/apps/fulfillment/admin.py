from django import forms
from django.contrib import admin, messages
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html

from apps.core.actors import Actor
from apps.core.exceptions import DomainError
from apps.sales.models import Order

from . import services
from .models import Fulfillment, FulfillmentStatus


class FulfillmentForm(forms.ModelForm):
    class Meta:
        model = Fulfillment
        fields = ("assigned_to", "driver_name", "vehicle", "recipient_name", "delivery_notes")


@admin.register(Fulfillment)
class FulfillmentAdmin(admin.ModelAdmin):
    """Created from an allocated order (action "Start picking" on Orders). Status moves by actions."""

    form = FulfillmentForm
    list_display = ("order", "method", "status", "assigned_to", "dispatched_at", "delivered_at", "pick_list")
    list_filter = ("status", "method")
    search_fields = ("order__number", "order__customer__name", "recipient_name")
    readonly_fields = (
        "order",
        "method",
        "status",
        "packed_at",
        "dispatched_at",
        "delivered_at",
        "failed_attempts",
        "released_unpaid_by",
        "pick_list",
    )
    actions = ("pack", "dispatch_or_hand_over", "delivered")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Pick list")
    def pick_list(self, obj):
        url = reverse("admin:fulfillment_pick_list", args=[obj.order_id])
        return format_html('<a href="{}" target="_blank">Print</a>', url)

    def get_urls(self):
        return [
            path(
                "pick-list/<uuid:order_id>/",
                self.admin_site.admin_view(self.pick_list_view),
                name="fulfillment_pick_list",
            ),
            *super().get_urls(),
        ]

    def pick_list_view(self, request, order_id):
        order = get_object_or_404(Order.objects.prefetch_related("lines"), pk=order_id)
        return TemplateResponse(request, "admin/fulfillment/pick_list.html", {"order": order})

    def _run(self, request, queryset, fn, done):
        for obj in queryset:
            try:
                fn(obj)
                self.message_user(request, f"{obj.order}: {done}.")
            except DomainError as exc:
                self.message_user(request, f"{obj.order}: {exc.message}", messages.ERROR)

    @admin.action(description="Mark packed", permissions=["change"])
    def pack(self, request, queryset):
        self._run(request, queryset, lambda f: services.mark_packed(f, Actor.for_user(request.user)), "packed")

    @admin.action(description="Dispatch / hand over (uses the recipient name on the record)", permissions=["change"])
    def dispatch_or_hand_over(self, request, queryset):
        self._run(
            request,
            queryset.filter(status=FulfillmentStatus.PACKED),
            lambda f: services.dispatch(
                f,
                Actor.for_user(request.user),
                user=request.user,
                driver_name=f.driver_name,
                vehicle=f.vehicle,
                recipient_name=f.recipient_name,
            ),
            "dispatched",
        )

    @admin.action(description="Mark delivered (uses the recipient name on the record)", permissions=["change"])
    def delivered(self, request, queryset):
        self._run(
            request,
            queryset,
            lambda f: services.mark_delivered(f, Actor.for_user(request.user), recipient_name=f.recipient_name),
            "delivered",
        )
