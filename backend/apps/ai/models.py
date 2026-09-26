"""Agent configuration and a complete record of every agent run, tool call and model call."""

from django.db import models

from apps.core.models import TimeStampedModel


class AgentConfig(models.Model):
    """Operational settings per agent, editable in the control centre. The agent's behaviour is code."""

    name = models.SlugField(max_length=40, primary_key=True)
    enabled = models.BooleanField(default=False, help_text="Agents ship disabled; switch on one at a time.")
    provider = models.CharField(max_length=20, default="anthropic")
    model = models.CharField(max_length=60, default="claude-opus-5")
    effort = models.CharField(max_length=10, default="medium", help_text="low | medium | high | xhigh | max")
    max_steps = models.PositiveSmallIntegerField(default=8, help_text="Model calls per run.")
    max_output_tokens = models.PositiveIntegerField(default=4000, help_text="Per model call.")
    timeout_seconds = models.PositiveIntegerField(default=120)
    daily_budget_usd = models.DecimalField(max_digits=8, decimal_places=2, default=5, help_text="Auto-disables above.")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} ({'on' if self.enabled else 'off'})"


class RunStatus(models.TextChoices):
    RUNNING = "RUNNING", "Running"
    SUCCEEDED = "SUCCEEDED", "Succeeded"
    ESCALATED = "ESCALATED", "Handed to a person"
    FAILED = "FAILED", "Failed"
    BLOCKED = "BLOCKED", "Not started (disabled / over budget)"


class AgentRun(TimeStampedModel):
    agent = models.CharField(max_length=40, db_index=True)
    status = models.CharField(max_length=10, choices=RunStatus.choices, default=RunStatus.RUNNING)
    trigger = models.CharField(max_length=60, help_text="e.g. whatsapp:inbound:123, beat:daily, admin")
    task = models.TextField(help_text="What the agent was asked to do.")
    output = models.TextField(blank=True)
    error = models.TextField(blank=True)
    steps = models.PositiveSmallIntegerField(default=0)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    estimated_cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    correlation_id = models.CharField(max_length=128, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.agent} run {self.created_at:%Y-%m-%d %H:%M} ({self.status})"


class ToolCallStatus(models.TextChoices):
    OK = "OK", "OK"
    ERROR = "ERROR", "Error (reported to the model)"
    DENIED = "DENIED", "Denied (not allowed)"
    PENDING_APPROVAL = "PENDING_APPROVAL", "Sent for approval"


class ToolCall(models.Model):
    id = models.BigAutoField(primary_key=True)
    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name="tool_calls")
    at = models.DateTimeField(auto_now_add=True)
    tool = models.CharField(max_length=60)
    tier = models.CharField(max_length=10)
    input = models.JSONField(default=dict)
    output = models.JSONField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=ToolCallStatus.choices)
    error = models.TextField(blank=True)
    duration_ms = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["at", "id"]

    def __str__(self):
        return f"{self.tool} ({self.status})"


class ModelCall(models.Model):
    """One request to an LLM provider: usage, latency, cost, outcome."""

    id = models.BigAutoField(primary_key=True)
    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name="model_calls")
    at = models.DateTimeField(auto_now_add=True)
    provider = models.CharField(max_length=20)
    model = models.CharField(max_length=60)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    latency_ms = models.PositiveIntegerField(default=0)
    estimated_cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    stop_reason = models.CharField(max_length=30, blank=True)
    ok = models.BooleanField(default=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["at", "id"]

    def __str__(self):
        return f"{self.model}: {self.input_tokens} in / {self.output_tokens} out"
