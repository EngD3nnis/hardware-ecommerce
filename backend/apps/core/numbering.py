"""Human-facing document numbers: PO-2026-00001, Q-2026-00001, SO-2026-00001…"""

from django.db import transaction

from .models import DocumentSequence
from .timeutils import business_today


def next_number(prefix: str) -> str:
    """Return the next number for `prefix` in the current business year.

    Must run inside the transaction that saves the document. If that
    transaction rolls back, the number is released as well, so sequences stay gapless.
    """
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("next_number() must be called inside transaction.atomic()")
    year = business_today().year
    sequence, _ = DocumentSequence.objects.select_for_update().get_or_create(prefix=prefix, year=year)
    sequence.last_value += 1
    sequence.save(update_fields=["last_value"])
    return f"{prefix}-{year}-{sequence.last_value:05d}"
