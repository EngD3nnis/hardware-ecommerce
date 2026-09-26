from celery import shared_task

from . import adapters, services


@shared_task(
    autoretry_for=(adapters.TransientSendError,),
    retry_backoff=30,
    retry_backoff_max=1800,
    max_retries=services.MAX_ATTEMPTS,
)
def send_message(message_id: str) -> str:
    return services.send(message_id).status
