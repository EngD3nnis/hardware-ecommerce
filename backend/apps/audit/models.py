from django.core.serializers.json import DjangoJSONEncoder
from django.db import models

from apps.core.actors import ActorType


class AuditEvent(models.Model):
    """Immutable record of a sensitive business event.

    Rows are written only through `apps.audit.services.record()`. A database
    trigger (migration 0002) rejects UPDATE and DELETE, so the history cannot be
    edited, even by a superuser or a bug. Actor and object references are
    stored as plain values rather than foreign keys, so deleting a user or
    object never needs to touch (or cascade into) its audit history.
    """

    id = models.BigAutoField(primary_key=True)
    at = models.DateTimeField(auto_now_add=True, db_index=True)

    actor_type = models.CharField(max_length=16, choices=ActorType.choices)
    actor_label = models.CharField(max_length=255)
    actor_user_id = models.CharField(max_length=64, blank=True, db_index=True)
    agent_run_id = models.CharField(max_length=64, blank=True)

    action = models.CharField(max_length=100, db_index=True)  # e.g. "catalog.product.updated"
    object_type = models.CharField(max_length=100, blank=True)  # "app_label.model"
    object_id = models.CharField(max_length=64, blank=True)
    object_repr = models.CharField(max_length=255, blank=True)

    before = models.JSONField(null=True, blank=True, encoder=DjangoJSONEncoder)
    after = models.JSONField(null=True, blank=True, encoder=DjangoJSONEncoder)
    reason = models.TextField(blank=True)

    correlation_id = models.CharField(max_length=128, blank=True, db_index=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-at", "-id"]
        indexes = [models.Index(fields=["object_type", "object_id"])]

    def __str__(self):
        return f"{self.at:%Y-%m-%d %H:%M} {self.actor_label}: {self.action} {self.object_repr}"
