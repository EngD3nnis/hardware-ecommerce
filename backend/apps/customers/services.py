import re

from django.db import IntegrityError, transaction

from apps.audit import services as audit
from apps.core.actors import Actor
from apps.core.exceptions import ValidationError

from .models import Customer

EDITABLE = ("name", "kind", "phone", "email", "kra_pin", "address", "whatsapp_opt_in", "notes")


def normalise_phone(raw: str) -> str:
    """Kenyan-friendly normalisation to E.164 digits: 0712345678 / +254 712 345 678 → 254712345678."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("0") and len(digits) == 10:
        digits = "254" + digits[1:]
    elif len(digits) == 9 and digits[0] in "17":
        digits = "254" + digits
    if not re.fullmatch(r"[1-9]\d{7,14}", digits):
        raise ValidationError(f"{raw!r} is not a valid phone number.", details={"phone": raw})
    return digits


@transaction.atomic
def create_customer(actor: Actor, *, name: str, phone: str = "", **fields) -> Customer:
    name = " ".join((name or "").split())
    if not name:
        raise ValidationError("Customer name is required.")
    unknown = set(fields) - set(EDITABLE)
    if unknown:
        raise ValidationError(f"Unknown customer fields: {', '.join(sorted(unknown))}")
    customer = Customer(name=name, phone=normalise_phone(phone) if phone else None, **fields)
    try:
        with transaction.atomic():
            customer.save()
    except IntegrityError as exc:
        raise ValidationError("A customer with this phone number already exists.", details={"phone": phone}) from exc
    audit.record(actor, "customers.customer.created", customer, after={"name": name, "phone": customer.phone})
    return customer


@transaction.atomic
def get_or_create_by_phone(phone: str, actor: Actor, *, name: str = "") -> tuple[Customer, bool]:
    """Find a customer by phone (any common format), or create one. Used by WhatsApp and the website."""
    normalised = normalise_phone(phone)
    existing = Customer.objects.filter(phone=normalised).first()
    if existing:
        return existing, False
    return create_customer(actor, name=name or f"Customer {normalised}", phone=normalised), True


@transaction.atomic
def update_customer(customer: Customer, changes: dict, actor: Actor) -> Customer:
    unknown = set(changes) - set(EDITABLE)
    if unknown:
        raise ValidationError(f"These fields cannot be changed: {', '.join(sorted(unknown))}")
    if "phone" in changes:
        changes["phone"] = normalise_phone(changes["phone"]) if changes["phone"] else None
    before = audit.snapshot(customer, changes.keys())
    for key, value in changes.items():
        setattr(customer, key, value)
    try:
        with transaction.atomic():
            customer.save()
    except IntegrityError as exc:
        raise ValidationError("Another customer already has this phone number.") from exc
    b, a = audit.diff(before, audit.snapshot(customer, changes.keys()))
    if a:
        audit.record(actor, "customers.customer.updated", customer, before=b, after=a)
    return customer
