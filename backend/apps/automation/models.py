"""Human control over automation: the kill switch, approvals, incidents and internal tasks.

None of this is needed for the shop to run. It exists so automation can be
switched off, and so consequential actions wait for a person (ADR 0004/0011).
"""

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.core.models import TimeStampedModel


class AutomationSwitch(models.Model):
    """Single row. all_disabled=True stops every agent run and tool call immediately."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    all_disabled = models.BooleanField(default=False)
    reason = models.CharField(max_length=255, blank=True)
    changed_by = models.CharField(max_length=255, blank=True)
    changed_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(id=1), name="automation_switch_singleton")]

    def __str__(self):
        return "Automations DISABLED" if self.all_disabled else "Automations enabled"


class ApprovalStatus(models.TextChoices):
    PENDING_APPROVAL = "PENDING_APPROVAL", "Waiting for approval"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    EXPIRED = "EXPIRED", "Expired"
    EXECUTED = "EXECUTED", "Approved and done"
    FAILED = "FAILED", "Approved, but failed to execute"


class ApprovalRequest(TimeStampedModel):
    """A consequential action proposed by an agent (or anyone), waiting for a person."""

    action = models.CharField(max_length=60, help_text="Registered action code, e.g. pricing.set_price")
    summary = models.CharField(max_length=300)
    proposed_values = models.JSONField(help_text="What was asked for.")
    final_values = models.JSONField(null=True, blank=True, help_text="What was actually executed (may be edited).")
    reason = models.TextField(blank=True, help_text="Why the requester wants this.")
    status = models.CharField(max_length=20, choices=ApprovalStatus.choices, default=ApprovalStatus.PENDING_APPROVAL)
    requested_by = models.CharField(max_length=255)
    agent_run_id = models.CharField(max_length=64, blank=True)
    decided_by = models.CharField(max_length=255, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)
    executed_at = models.DateTimeField(null=True, blank=True)
    result = models.JSONField(null=True, blank=True)
    error = models.TextField(blank=True)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ["status", "-created_at"]
        permissions = [("decide_approvalrequest", "Can approve or reject automation requests")]

    def __str__(self):
        return f"{self.summary} ({self.get_status_display()})"


class Severity(models.TextChoices):
    INFO = "INFO", "Info"
    WARNING = "WARNING", "Warning"
    CRITICAL = "CRITICAL", "Critical"


class IncidentStatus(models.TextChoices):
    OPEN = "OPEN", "Open"
    ACKNOWLEDGED = "ACKNOWLEDGED", "Acknowledged"
    RESOLVED = "RESOLVED", "Resolved"


class Incident(TimeStampedModel):
    """Something a person should look at: stuck order, failed messages, inventory mismatch, agent failure…

    Deduplicated by fingerprint while open: a repeating problem bumps `occurrences`.
    """

    kind = models.CharField(max_length=40)
    severity = models.CharField(max_length=10, choices=Severity.choices, default=Severity.WARNING)
    title = models.CharField(max_length=300)
    details = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=12, choices=IncidentStatus.choices, default=IncidentStatus.OPEN)
    fingerprint = models.CharField(max_length=200)
    source = models.CharField(max_length=60, help_text="What raised it: a check, an agent, a task.")
    occurrences = models.PositiveIntegerField(default=1)
    last_seen_at = models.DateTimeField(auto_now_add=True)
    resolved_by = models.CharField(max_length=255, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["status", "-last_seen_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["fingerprint"], condition=~Q(status="RESOLVED"), name="one_unresolved_incident_per_fingerprint"
            )
        ]

    def __str__(self):
        return f"[{self.severity}] {self.title}"


class TaskStatus(models.TextChoices):
    OPEN = "OPEN", "Open"
    DONE = "DONE", "Done"
    CANCELLED = "CANCELLED", "Cancelled"


class InternalTask(TimeStampedModel):
    """A to-do for staff, e.g. "call this customer back", raised by an agent or a check."""

    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=TaskStatus.choices, default=TaskStatus.OPEN)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    related_type = models.CharField(max_length=60, blank=True)
    related_id = models.CharField(max_length=64, blank=True)
    created_by = models.CharField(max_length=255)
    due_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["status", "-created_at"]

    def __str__(self):
        return self.title
