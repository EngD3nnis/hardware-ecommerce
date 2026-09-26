"""Kill switch, approvals, incidents and internal tasks."""

import logging
from collections.abc import Callable
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from apps.audit import services as audit
from apps.core.actors import Actor
from apps.core.exceptions import Conflict, DomainError, PermissionDenied, ValidationError

from .models import (
    ApprovalRequest,
    ApprovalStatus,
    AutomationSwitch,
    Incident,
    IncidentStatus,
    InternalTask,
    Severity,
)

logger = logging.getLogger(__name__)
SWITCH_CACHE_KEY = "automation:all_disabled"
APPROVAL_TTL = timedelta(days=3)


# --- Kill switch ----------------------------------------------------------------------------------


def automations_enabled() -> bool:
    """False if AUTOMATION_HARD_DISABLE is set or the admin switch is off. The core app never calls this."""
    if settings.AUTOMATION_HARD_DISABLE:
        return False
    disabled = cache.get(SWITCH_CACHE_KEY)
    if disabled is None:
        switch = AutomationSwitch.objects.filter(pk=1).first()
        disabled = bool(switch and switch.all_disabled)
        cache.set(SWITCH_CACHE_KEY, disabled, timeout=10)
    return not disabled


def set_all_disabled(disabled: bool, actor: Actor, reason: str = "") -> AutomationSwitch:
    switch, _ = AutomationSwitch.objects.get_or_create(pk=1)
    switch.all_disabled, switch.reason, switch.changed_by = disabled, reason[:255], actor.label
    switch.save()
    cache.delete(SWITCH_CACHE_KEY)
    audit.record(actor, "automation.switch", switch, after={"all_disabled": disabled}, reason=reason)
    logger.warning("Automation switch changed", extra={"all_disabled": disabled, "by": actor.label})
    return switch


# --- Approvals --------------------------------------------------------------------------------------

# action code -> (executor(values, approver_actor, approver_user) -> result dict)
_ACTIONS: dict[str, Callable] = {}


def register_action(code: str):
    """Register the function that performs an approved action. It runs as the approving person."""

    def decorator(fn):
        _ACTIONS[code] = fn
        return fn

    return decorator


def registered_actions() -> list[str]:
    return sorted(_ACTIONS)


def request_approval(
    action: str, *, summary: str, values: dict, requested_by: Actor, reason: str = ""
) -> ApprovalRequest:
    if action not in _ACTIONS:
        raise ValidationError(f"Unknown action {action!r}.")
    request = ApprovalRequest.objects.create(
        action=action,
        summary=summary[:300],
        proposed_values=values,
        reason=reason,
        requested_by=requested_by.label,
        agent_run_id=requested_by.agent_run_id or "",
        expires_at=timezone.now() + APPROVAL_TTL,
    )
    audit.record(requested_by, "automation.approval.requested", request, after={"action": action, "values": values})
    return request


@transaction.atomic
def approve(request: ApprovalRequest, user, *, final_values: dict | None = None, note: str = "") -> ApprovalRequest:
    """A person approves; the action executes immediately, as that person."""
    actor = Actor.for_user(user)
    if not user.has_perm("automation.decide_approvalrequest"):
        raise PermissionDenied("You do not have permission to approve automation requests.")
    request = ApprovalRequest.objects.select_for_update().get(pk=request.pk)
    if request.status != ApprovalStatus.PENDING_APPROVAL:
        raise Conflict(f"This request is already {request.get_status_display().lower()}.")
    if request.expires_at <= timezone.now():
        request.status = ApprovalStatus.EXPIRED
        request.save(update_fields=["status", "updated_at"])
        raise Conflict("This request has expired.")
    values = final_values if final_values is not None else request.proposed_values
    request.final_values, request.decided_by, request.decided_at = values, actor.label, timezone.now()
    request.decision_note = note
    try:
        with transaction.atomic():
            result = _ACTIONS[request.action](values, actor, user)
        request.status, request.result, request.executed_at = ApprovalStatus.EXECUTED, result, timezone.now()
    except DomainError as exc:
        request.status, request.error = ApprovalStatus.FAILED, exc.message
    request.save()
    audit.record(
        actor,
        "automation.approval.decided",
        request,
        after={"status": request.status, "values": values},
        reason=note,
    )
    return request


@transaction.atomic
def reject(request: ApprovalRequest, user, note: str = "") -> ApprovalRequest:
    if not user.has_perm("automation.decide_approvalrequest"):
        raise PermissionDenied("You do not have permission to reject automation requests.")
    request = ApprovalRequest.objects.select_for_update().get(pk=request.pk)
    if request.status != ApprovalStatus.PENDING_APPROVAL:
        raise Conflict(f"This request is already {request.get_status_display().lower()}.")
    request.status, request.decided_by, request.decided_at = (
        ApprovalStatus.REJECTED,
        user.get_username(),
        timezone.now(),
    )
    request.decision_note = note
    request.save()
    audit.record(
        Actor.for_user(user), "automation.approval.decided", request, after={"status": "REJECTED"}, reason=note
    )
    return request


def expire_approvals() -> int:
    return ApprovalRequest.objects.filter(
        status=ApprovalStatus.PENDING_APPROVAL, expires_at__lte=timezone.now()
    ).update(status=ApprovalStatus.EXPIRED)


# --- Incidents & tasks ------------------------------------------------------------------------------------


def raise_incident(
    *, kind: str, title: str, fingerprint: str, source: str, severity: str = Severity.WARNING, details=None
) -> Incident:
    """Open an incident, or bump the open one with the same fingerprint. Safe to call repeatedly."""
    existing = Incident.objects.exclude(status=IncidentStatus.RESOLVED).filter(fingerprint=fingerprint).first()
    if existing:
        Incident.objects.filter(pk=existing.pk).update(
            occurrences=F("occurrences") + 1, last_seen_at=timezone.now(), details=details or existing.details
        )
        existing.refresh_from_db()
        return existing
    try:
        with transaction.atomic():
            incident = Incident.objects.create(
                kind=kind,
                title=title[:300],
                fingerprint=fingerprint[:200],
                source=source,
                severity=severity,
                details=details or {},
            )
    except IntegrityError:
        return Incident.objects.exclude(status=IncidentStatus.RESOLVED).get(fingerprint=fingerprint)
    log = logger.error if severity == Severity.CRITICAL else logger.warning
    log("Incident raised", extra={"kind": kind, "title": title})
    return incident


def resolve_incident(incident: Incident, actor: Actor, note: str = "") -> Incident:
    incident.status, incident.resolved_by, incident.resolved_at = IncidentStatus.RESOLVED, actor.label, timezone.now()
    incident.save(update_fields=["status", "resolved_by", "resolved_at", "updated_at"])
    audit.record(actor, "automation.incident.resolved", incident, reason=note)
    return incident


def create_task(*, title: str, description: str = "", created_by: Actor, related=None) -> InternalTask:
    task = InternalTask.objects.create(
        title=title[:200],
        description=description,
        created_by=created_by.label,
        related_type=related._meta.label_lower if related is not None else "",
        related_id=str(related.pk) if related is not None else "",
    )
    return task
