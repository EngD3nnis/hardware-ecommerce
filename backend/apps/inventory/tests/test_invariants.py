"""Property-based test: random sequences of stock operations never break the invariants."""

from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from apps.catalog.tests.factories import ProductFactory
from apps.core.actors import Actor
from apps.core.exceptions import DomainError
from apps.inventory import services
from apps.inventory.models import Reservation, ReservationStatus, StockBalance
from apps.inventory.services import Source

OPERATIONS = st.lists(
    st.tuples(
        st.sampled_from(
            ["receive", "remove", "reserve", "release", "consume", "damage", "write_off", "recover", "return"]
        ),
        st.integers(min_value=1, max_value=6),
    ),
    max_size=40,
)


@pytest.mark.django_db
@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(operations=OPERATIONS)
def test_invariants_hold_for_any_sequence(operations):
    actor = Actor.system("hypothesis")
    product = ProductFactory()
    for i, (op, qty) in enumerate(operations):
        active = list(Reservation.objects.filter(product=product, status=ReservationStatus.ACTIVE))
        try:
            if op == "receive":
                services.receive(product, qty, actor)
            elif op == "remove":
                services.remove(product, qty, actor)
            elif op == "reserve":
                services.reserve(product, qty, actor, source=Source("t", str(i)))
            elif op == "release" and active:
                services.release(active[0], actor)
            elif op == "consume" and active:
                services.consume(active[-1], actor)
            elif op == "damage":
                services.mark_damaged(product, qty, actor)
            elif op == "write_off":
                services.write_off_damaged(product, qty, actor)
            elif op == "recover":
                services.recover_damaged(product, qty, actor)
            elif op == "return":
                services.return_to_stock(product, qty, actor, sellable=qty % 2 == 0)
        except DomainError:
            pass  # refusing an impossible operation is correct behaviour

        balance = StockBalance.objects.filter(product=product).first()
        if balance is None:
            continue
        # Invariants:
        assert balance.on_hand >= 0 and balance.damaged >= 0 and balance.reserved >= 0
        assert balance.available <= balance.on_hand  # available never exceeds physical
        assert balance.reserved <= balance.on_hand
        active_total = sum(
            (r.quantity for r in Reservation.objects.filter(product=product, status=ReservationStatus.ACTIVE)),
            Decimal("0"),
        )
        assert balance.reserved == active_total
    assert services.reconcile() == [] or all(m["sku"] != product.sku for m in services.reconcile())
