from django.contrib import admin, messages

from apps.core.actors import Actor
from apps.core.exceptions import DomainError

from . import services
from .models import ApprovalRequest, ApprovalStatus, AutomationSwitch, Incident, IncidentStatus, InternalTask


@admin.register(ApprovalRequest)
class ApprovalRequestAdmin(admin.ModelAdmin):
    list_display = ("created_at", "summary", "action", "requested_by", "status", "decided_by")
    list_filter = ("status", "action")
    search_fields = ("summary", "requested_by", "agent_run_id")
    readonly_fields = [f.name for f in ApprovalRequest._meta.fields]
    actions = ("approve_selected", "reject_selected")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_decide_permission(self, request):
        return request.user.has_perm("automation.decide_approvalrequest")

    @admin.action(description="Approve and execute", permissions=["decide"])
    def approve_selected(self, request, queryset):
        for item in queryset.filter(status=ApprovalStatus.PENDING_APPROVAL):
            try:
                done = services.approve(item, request.user, note="Approved in admin")
                level = messages.SUCCESS if done.status == ApprovalStatus.EXECUTED else messages.ERROR
                self.message_user(request, f"{item.summary}: {done.get_status_display()} {done.error}", level)
            except DomainError as exc:
                self.message_user(request, f"{item.summary}: {exc.message}", messages.ERROR)

    @admin.action(description="Reject", permissions=["decide"])
    def reject_selected(self, request, queryset):
        for item in queryset.filter(status=ApprovalStatus.PENDING_APPROVAL):
            services.reject(item, request.user, note="Rejected in admin")
        self.message_user(request, "Rejected.")


@admin.register(Incident)
class IncidentAdmin(admin.ModelAdmin):
    list_display = ("last_seen_at", "severity", "title", "kind", "occurrences", "status", "source")
    list_filter = ("status", "severity", "kind")
    search_fields = ("title", "fingerprint")
    readonly_fields = [f.name for f in Incident._meta.fields if f.name != "status"]
    actions = ("resolve",)

    def has_add_permission(self, request):
        return False

    @admin.action(description="Mark resolved", permissions=["change"])
    def resolve(self, request, queryset):
        for incident in queryset.exclude(status=IncidentStatus.RESOLVED):
            services.resolve_incident(incident, Actor.for_user(request.user))
        self.message_user(request, "Resolved.")


@admin.register(InternalTask)
class InternalTaskAdmin(admin.ModelAdmin):
    list_display = ("created_at", "title", "status", "assigned_to", "created_by")
    list_filter = ("status",)
    search_fields = ("title", "description")
    readonly_fields = ("created_by", "related_type", "related_id")


@admin.register(AutomationSwitch)
class AutomationSwitchAdmin(admin.ModelAdmin):
    """Read-only here; use the control centre (/ops/) button, which also clears the cache and audits."""

    list_display = ("__str__", "changed_by", "changed_at", "reason")
    readonly_fields = ("all_disabled", "reason", "changed_by", "changed_at")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
