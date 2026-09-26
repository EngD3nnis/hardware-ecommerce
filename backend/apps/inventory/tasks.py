import logging

from celery import shared_task

from . import services

logger = logging.getLogger(__name__)


@shared_task
def release_expired_reservations() -> int:
    released = services.release_expired()
    if released:
        logger.info("Released expired reservations", extra={"count": released})
    return released
