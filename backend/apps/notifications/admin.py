from django.contrib import admin

from . import services
from .models import InboundMessage, MessageStatus, OutboundMessage


@admin.register(OutboundMessage)
class OutboundMessageAdmin(admin.ModelAdmin):
    list_display = ("created_at", "channel", "to", "template", "status", "attempts", "backend", "last_error")
    list_filter = ("status", "channel", "template", "backend")
    search_fields = ("to", "body", "related_id", "customer__name")
    readonly_fields = [f.name for f in OutboundMessage._meta.fields]
    actions = ("retry_failed", "cancel_queued")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Retry failed messages", permissions=["change"])
    def retry_failed(self, request, queryset):
        for message in queryset.filter(status=MessageStatus.FAILED):
            services.retry(message)
        self.message_user(request, "Requeued.")

    @admin.action(description="Cancel queued messages", permissions=["change"])
    def cancel_queued(self, request, queryset):
        n = queryset.filter(status=MessageStatus.QUEUED).update(status=MessageStatus.CANCELLED)
        self.message_user(request, f"Cancelled {n}.")


@admin.register(InboundMessage)
class InboundMessageAdmin(admin.ModelAdmin):
    list_display = ("received_at", "from_phone", "customer", "body", "handled", "handled_by")
    list_filter = ("handled", "message_type")
    search_fields = ("from_phone", "body", "customer__name")
    readonly_fields = (
        "received_at",
        "from_phone",
        "customer",
        "body",
        "message_type",
        "payload",
        "provider_message_id",
        "handled_by",
    )
    fields = (*readonly_fields, "handled")

    def has_add_permission(self, request):
        return False

    def save_model(self, request, obj, form, change):
        if "handled" in form.changed_data and obj.handled:
            obj.handled_by = request.user.get_username()
        super().save_model(request, obj, form, change)
