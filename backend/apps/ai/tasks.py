"""Agent triggers. Each checks the kill switch and the agent's switch first, so a disabled agent costs nothing."""

from celery import shared_task

from apps.automation.services import automations_enabled
from apps.core.timeutils import business_today
from apps.notifications.models import InboundMessage, OutboundMessage

from . import runner
from .models import AgentConfig, AgentRun


def _active(name: str) -> bool:
    return automations_enabled() and AgentConfig.objects.filter(name=name, enabled=True).exists()


def _conversation(phone: str, limit: int = 8) -> str:
    """Recent messages with this customer, oldest first, for context."""
    inbound = [(m.received_at, "Customer", m.body) for m in InboundMessage.objects.filter(from_phone=phone)[:limit]]
    outbound = [(m.created_at, "Dewmix", m.body) for m in OutboundMessage.objects.filter(to=phone)[:limit]]
    lines = sorted(inbound + outbound)[-limit:]
    return "\n".join(f"{who}: {text}" for _, who, text in lines)


@shared_task(soft_time_limit=180, time_limit=200)
def handle_inbound_message(inbound_id: int) -> str:
    if not _active("sales"):
        return "skipped"
    trigger = f"whatsapp:inbound:{inbound_id}"
    if AgentRun.objects.filter(trigger=trigger).exists():
        return "duplicate"  # task retried or delivered twice
    message = InboundMessage.objects.filter(pk=inbound_id, handled=False).first()
    if message is None:
        return "already handled"
    customer = message.customer.name if message.customer else "unknown (new customer)"
    task = (
        f"A customer wrote on WhatsApp.\nPhone: {message.from_phone}\nCustomer record: {customer}\n"
        f"Inbound message id (for reply_to_customer): {message.pk}\n\nConversation so far:\n"
        f"{_conversation(message.from_phone)}\n\nLatest message: {message.body or '(non-text message)'}"
    )
    return runner.run_agent("sales", task, trigger=trigger).status


@shared_task(soft_time_limit=600, time_limit=620)
def run_scheduled_agent(name: str) -> str:
    if not _active(name):
        return "skipped"
    today = business_today().isoformat()
    tasks = {
        "inventory": "Do today's stock review and prepare replenishment drafts.",
        "catalogue": "Work through the open catalogue issues (up to 20) and propose corrections.",
        "operations": "Do the operations check and write today's summary for the owner.",
    }
    return runner.run_agent(name, tasks[name], trigger=f"beat:{name}:{today}").status
