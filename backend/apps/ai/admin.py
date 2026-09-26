from django.contrib import admin

from .models import AgentConfig, AgentRun, ModelCall, ToolCall


@admin.register(AgentConfig)
class AgentConfigAdmin(admin.ModelAdmin):
    list_display = ("name", "enabled", "provider", "model", "effort", "max_steps", "daily_budget_usd")
    list_editable = ("enabled",)


class ToolCallInline(admin.TabularInline):
    model = ToolCall
    extra = 0
    can_delete = False
    fields = ("at", "tool", "tier", "status", "input", "output", "error", "duration_ms")
    readonly_fields = fields


class ModelCallInline(admin.TabularInline):
    model = ModelCall
    extra = 0
    can_delete = False
    fields = (
        "at",
        "model",
        "input_tokens",
        "output_tokens",
        "latency_ms",
        "estimated_cost_usd",
        "stop_reason",
        "ok",
        "error",
    )
    readonly_fields = fields


@admin.register(AgentRun)
class AgentRunAdmin(admin.ModelAdmin):
    """Full record of what each agent was asked, what it called, and what it cost."""

    list_display = ("created_at", "agent", "status", "trigger", "steps", "estimated_cost_usd", "error")
    list_filter = ("agent", "status")
    search_fields = ("task", "output", "error", "correlation_id")
    readonly_fields = [f.name for f in AgentRun._meta.fields]
    inlines = (ModelCallInline, ToolCallInline)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
