"""Background jobs for catalogue imports. Thin wrappers: the logic is in imports.py.

Tasks take ids and plain values (not objects) and are safe to run twice:
each import step checks the batch status before doing anything.
"""

import logging

from celery import shared_task

from apps.core.actors import Actor, ActorType

from . import imports
from .models import ImportBatch

logger = logging.getLogger(__name__)


def _actor(actor_data: dict) -> Actor:
    return Actor(type=ActorType(actor_data["type"]), label=actor_data["label"], user_id=actor_data.get("user_id"))


def actor_payload(actor: Actor) -> dict:
    return {"type": actor.type.value, "label": actor.label, "user_id": actor.user_id}


@shared_task(soft_time_limit=15 * 60, time_limit=16 * 60)
def validate_import_batch(batch_id: str) -> str:
    batch = imports.validate_batch(ImportBatch.objects.get(pk=batch_id))
    logger.info("Import batch validated", extra={"batch_id": batch_id, "status": batch.status})
    return batch.status


@shared_task(soft_time_limit=15 * 60, time_limit=16 * 60)
def commit_import_batch(batch_id: str, actor_data: dict) -> str:
    batch = imports.commit_batch(ImportBatch.objects.get(pk=batch_id), _actor(actor_data))
    logger.info("Import batch commit finished", extra={"batch_id": batch_id, "status": batch.status})
    return batch.status


@shared_task(soft_time_limit=15 * 60, time_limit=16 * 60)
def rollback_import_batch(batch_id: str, actor_data: dict) -> dict:
    result = imports.rollback_batch(ImportBatch.objects.get(pk=batch_id), _actor(actor_data))
    logger.info("Import batch rolled back", extra={"batch_id": batch_id})
    return result
