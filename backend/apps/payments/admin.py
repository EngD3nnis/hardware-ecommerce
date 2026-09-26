from django import forms
from django.contrib import admin, messages
from django.http import HttpResponseRedirect

from apps.core.actors import Actor
from apps.core.exceptions import DomainError

from . import services
from .models import EventStatus, InboundPaymentEvent, Payment, Refund


class _Rejected(Exception):
    pass


class RecordThroughServiceAdmin(admin.ModelAdmin):
    """Add form calls a service; the saved record is read-only afterwards."""

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields] if obj else []

    def has_change_permission(self, request, obj=None):
        return obj is None and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return False

    def add_view(self, request, form_url="", extra_context=None):
        try:
            return super().add_view(request, form_url, extra_context)
        except _Rejected:
            return HttpResponseRedirect(request.get_full_path())

    def call(self, request, fn):
        try:
            return fn()
        except DomainError as exc:
            self.message_user(request, exc.message, messages.ERROR)
            raise _Rejected from exc


class PaymentForm(forms.ModelForm):
    class Meta:
        model = Payment
        fields = ("order", "method", "amount", "provider_reference")
        labels = {"provider_reference": "M-Pesa / bank reference"}


@admin.register(Payment)
class PaymentAdmin(RecordThroughServiceAdmin):
    form = PaymentForm
    list_display = ("order", "method", "amount", "state", "provider_reference", "received_at", "recorded_by")
    list_filter = ("state", "method")
    search_fields = ("order__number", "provider_reference", "provider_request_id", "payer_phone")
    autocomplete_fields = ("order",)

    def save_model(self, request, obj, form, change):
        data = form.cleaned_data
        payment = self.call(
            request,
            lambda: services.record_payment(
                data["order"],
                Actor.for_user(request.user),
                method=data["method"],
                amount=data["amount"],
                reference=data.get("provider_reference") or "",
            ),
        )
        obj.pk, obj._state.adding = payment.pk, False


class RefundForm(forms.ModelForm):
    class Meta:
        model = Refund
        fields = ("payment", "amount", "reason", "reference")


@admin.register(Refund)
class RefundAdmin(RecordThroughServiceAdmin):
    form = RefundForm
    list_display = ("payment", "amount", "reason", "reference", "recorded_by", "created_at")
    search_fields = ("payment__order__number", "reference")
    autocomplete_fields = ("payment",)

    def has_add_permission(self, request):
        return request.user.has_perm("payments.record_refund")

    def save_model(self, request, obj, form, change):
        data = form.cleaned_data
        refund = self.call(
            request,
            lambda: services.record_refund(
                data["payment"],
                Actor.for_user(request.user),
                amount=data["amount"],
                reason=data["reason"],
                reference=data.get("reference") or "",
                user=request.user,
            ),
        )
        obj.pk, obj._state.adding = refund.pk, False


@admin.register(InboundPaymentEvent)
class InboundPaymentEventAdmin(admin.ModelAdmin):
    list_display = ("received_at", "provider", "event_id", "status", "error")
    list_filter = ("provider", "status")
    search_fields = ("event_id",)
    readonly_fields = [f.name for f in InboundPaymentEvent._meta.fields]
    actions = ("reprocess",)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Process again (failed/received only)", permissions=["change"])
    def reprocess(self, request, queryset):
        for event in queryset.filter(status__in=[EventStatus.RECEIVED, EventStatus.FAILED]):
            InboundPaymentEvent.objects.filter(pk=event.pk).update(status=EventStatus.RECEIVED, error="")
            services.process_mpesa_event(event)
        self.message_user(request, "Reprocessed.")
