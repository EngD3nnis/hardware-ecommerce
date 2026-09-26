from celery import shared_task

from . import services


@shared_task
def refresh_daily_snapshots() -> int:
    return services.refresh_recent(days=3)
