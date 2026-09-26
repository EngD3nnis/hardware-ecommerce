from celery import shared_task

from . import checks


@shared_task
def run_operational_checks() -> dict:
    return checks.run_all()


@shared_task(soft_time_limit=20 * 60, time_limit=21 * 60)
def reconcile_inventory() -> int:
    return checks.inventory_reconciliation()
