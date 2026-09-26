from django.contrib import admin

from .models import AuditEvent


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    """Read-only. Nobody can add, edit or delete audit events here (or anywhere)."""

    list_display = ("at", "actor_type", "actor_label", "action", "object_type", "object_repr", "correlation_id")
    list_filter = ("actor_type", "action", "object_type")
    search_fields = ("actor_label", "action", "object_id", "object_repr", "correlation_id", "reason")
    date_hierarchy = "at"
    list_per_page = 100

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
