from celery import shared_task

from . import services


@shared_task
def expire_quotations() -> int:
    return services.expire_quotations()
