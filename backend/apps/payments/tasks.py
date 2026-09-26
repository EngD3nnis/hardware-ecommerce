from celery import shared_task

from . import services
from .models import EventStatus, InboundPaymentEvent


@shared_task(autoretry_for=(ConnectionError,), retry_backoff=True, max_retries=5)
def process_payment_event(event_id: int) -> str:
    event = InboundPaymentEvent.objects.get(pk=event_id)
    return services.process_mpesa_event(event).status


@shared_task
def process_stuck_payment_events() -> int:
    """Safety net: process events whose task never ran (e.g. broker was down)."""
    count = 0
    for event in InboundPaymentEvent.objects.filter(status=EventStatus.RECEIVED):
        services.process_mpesa_event(event)
        count += 1
    return count
