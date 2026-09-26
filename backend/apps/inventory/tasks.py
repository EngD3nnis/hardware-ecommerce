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


@shared_task(soft_time_limit=20 * 60, time_limit=21 * 60)
def reconcile_inventory() -> int:
    """Compare balances with the ledger. Mismatches are logged at ERROR (→ Sentry); never auto-fixed."""
    mismatches = services.reconcile()
    if mismatches:
        logger.error("Inventory reconciliation found mismatches", extra={"mismatches": mismatches[:50]})
    return len(mismatches)
