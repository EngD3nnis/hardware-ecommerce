"""Automation control centre (/ops/): staff-only, server-rendered."""

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count, Sum
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.automation import services as automation
from apps.automation.models import (
    ApprovalRequest,
    ApprovalStatus,
    AutomationSwitch,
    Incident,
    IncidentStatus,
    InternalTask,
)
from apps.core.actors import Actor

from .agents import AGENTS
from .models import AgentConfig, AgentRun, RunStatus


@staff_member_required
def control_centre(request):
    today = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    configs = {c.name: c for c in AgentConfig.objects.all()}
    usage = {
        row["agent"]: row
        for row in AgentRun.objects.filter(created_at__gte=today)
        .values("agent")
        .annotate(
            runs=Count("id"),
            cost=Sum("estimated_cost_usd"),
            tokens_in=Sum("input_tokens"),
            tokens_out=Sum("output_tokens"),
        )
    }
    agents = []
    for name, definition in AGENTS.items():
        config = configs.get(name) or AgentConfig(name=name)
        last = AgentRun.objects.filter(agent=name).first()
        agents.append(
            {
                "name": name,
                "purpose": definition.purpose,
                "config": config,
                "today": usage.get(name, {}),
                "last": last,
                "tools": sorted(definition.tools),
                "max_tier": definition.max_tier.name,
            }
        )
    context = {
        "switch": AutomationSwitch.objects.filter(pk=1).first(),
        "hard_disabled": not automation.automations_enabled(),
        "agents": agents,
        "recent_runs": AgentRun.objects.all()[:25],
        "failures": AgentRun.objects.filter(status=RunStatus.FAILED)[:10],
        "approvals": ApprovalRequest.objects.filter(status=ApprovalStatus.PENDING_APPROVAL)[:25],
        "incidents": Incident.objects.exclude(status=IncidentStatus.RESOLVED)[:25],
        "tasks": InternalTask.objects.filter(status="OPEN")[:25],
        "can_control": request.user.has_perm("ai.change_agentconfig"),
    }
    return render(request, "ai/control_centre.html", context)


@staff_member_required
@require_POST
def toggle_all(request):
    if not request.user.has_perm("ai.change_agentconfig"):
        messages.error(request, "You do not have permission to change automation settings.")
        return redirect("ops-control-centre")
    disable = request.POST.get("disable") == "1"
    automation.set_all_disabled(disable, Actor.for_user(request.user), reason=request.POST.get("reason", ""))
    messages.warning(request, "ALL automations disabled." if disable else "Automations enabled.")
    return redirect("ops-control-centre")


@staff_member_required
@require_POST
def toggle_agent(request, name):
    if name not in AGENTS or not request.user.has_perm("ai.change_agentconfig"):
        messages.error(request, "Not allowed.")
        return redirect("ops-control-centre")
    config, _ = AgentConfig.objects.get_or_create(name=name)
    config.enabled = not config.enabled
    config.save(update_fields=["enabled", "updated_at"])
    from apps.audit.services import record

    record(Actor.for_user(request.user), "ai.agent.toggled", config, after={"enabled": config.enabled})
    messages.info(request, f"Agent {name} {'enabled' if config.enabled else 'disabled'}.")
    return redirect("ops-control-centre")
