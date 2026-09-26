"""Writing to the audit log.

Call `record()` from services, inside the same transaction as the change it
describes, so the audit entry exists if and only if the change committed.
"""

from collections.abc import Iterable

from django.db import models

from apps.core.actors import Actor
from apps.core.request_context import get_client_meta, get_correlation_id

from .models import AuditEvent


def snapshot(instance: models.Model, fields: Iterable[str]) -> dict:
    """Plain-value copy of selected fields; foreign keys become their ids."""
    data = {}
    for name in fields:
        field = instance._meta.get_field(name)
        if isinstance(field, models.ForeignKey):
            value = getattr(instance, field.attname)
            data[name] = str(value) if value is not None else None
        else:
            data[name] = getattr(instance, name)
    return data


def diff(before: dict, after: dict) -> tuple[dict, dict]:
    """Reduce two snapshots to only the keys whose values changed."""
    changed = [k for k in after if before.get(k) != after.get(k)]
    return {k: before.get(k) for k in changed}, {k: after[k] for k in changed}


def record(
    actor: Actor,
    action: str,
    obj: models.Model | None = None,
    *,
    before: dict | None = None,
    after: dict | None = None,
    reason: str = "",
    object_type: str = "",
    object_id: str = "",
) -> AuditEvent:
    if obj is not None:
        object_type = object_type or obj._meta.label_lower
        object_id = object_id or str(obj.pk)
    meta = get_client_meta()
    return AuditEvent.objects.create(
        actor_type=actor.type,
        actor_label=actor.label[:255],
        actor_user_id=actor.user_id or "",
        agent_run_id=actor.agent_run_id or "",
        action=action,
        object_type=object_type,
        object_id=object_id,
        object_repr=str(obj)[:255] if obj is not None else "",
        before=before,
        after=after,
        reason=reason,
        correlation_id=get_correlation_id() or "",
        ip=meta.ip,
        user_agent=meta.user_agent,
    )
