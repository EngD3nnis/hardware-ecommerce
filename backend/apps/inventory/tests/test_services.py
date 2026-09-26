from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import InternalError, ProgrammingError, transaction
from django.utils import timezone

from apps.catalog.models import UnitOfMeasure
from apps.catalog.tests.factories import ProductFactory
from apps.core.exceptions import Conflict, ValidationError
from apps.inventory import services
from apps.inventory.models import (
    AdjustmentKind,
    MovementReason,
    ReservationStatus,
    StockBalance,
    StockLocation,
    StockMovement,
)
from apps.inventory.services import InsufficientStock, Source

pytestmark = pytest.mark.django_db

ORDER = Source("orders.order", "o-1")


@pytest.fixture
def product():
    return ProductFactory()


@pytest.fixture
def shop():
    return StockLocation.objects.get(is_default=True)


def balance(product, location=None):
    return StockBalance.objects.get(product=product, location=location or services.default_location())


def test_default_location_is_seeded(shop):
    assert (shop.code, shop.name) == ("kenol", "Kenol shop")


class TestQuantities:
    @pytest.mark.parametrize("qty", ["0", "-1", "abc", "1.0001", "NaN"])
    def test_rejects_bad_quantities(self, product, system_actor, qty):
        with pytest.raises(ValidationError):
            services.receive(product, qty, system_actor)

    def test_countable_units_need_whole_numbers(self, product, system_actor):
        with pytest.raises(ValidationError, match="whole number"):
            services.receive(product, "1.5", system_actor)

    def test_metres_may_be_fractional(self, system_actor):
        hose = ProductFactory(unit_of_measure=UnitOfMeasure.METRE)
        services.receive(hose, "12.5", system_actor)
        assert balance(hose).on_hand == Decimal("12.5")


class TestLedger:
    def test_receive_writes_movement_and_balance(self, product, system_actor):
        movement = services.receive(product, 10, system_actor, unit_cost="120.00", note="PO-1")
        assert movement.quantity == 10 and movement.balance_after == 10
        assert movement.reason == MovementReason.PURCHASE_RECEIVED
        assert (movement.actor_type, movement.actor_label) == ("SYSTEM", "test")
        assert balance(product).on_hand == 10

    def test_idempotency_key_prevents_double_receipt(self, product, system_actor):
        first = services.receive(product, 10, system_actor, idempotency_key="grn-1")
        again = services.receive(product, 10, system_actor, idempotency_key="grn-1")
        assert first.pk == again.pk
        assert balance(product).on_hand == 10
        assert StockMovement.objects.count() == 1

    def test_cannot_remove_more_than_available(self, product, system_actor):
        services.receive(product, 5, system_actor)
        services.reserve(product, 3, system_actor, source=ORDER)
        with pytest.raises(InsufficientStock):
            services.remove(product, 3, system_actor, note="lost")
        services.remove(product, 2, system_actor, note="lost")
        assert (balance(product).on_hand, balance(product).available) == (3, 0)

    def test_damage_moves_between_buckets(self, product, system_actor):
        services.receive(product, 10, system_actor)
        services.mark_damaged(product, 3, system_actor, note="dropped")
        b = balance(product)
        assert (b.on_hand, b.damaged, b.available) == (7, 3, 7)
        services.recover_damaged(product, 1, system_actor)
        services.write_off_damaged(product, 2, system_actor)
        b = balance(product)
        assert (b.on_hand, b.damaged) == (8, 0)
        with pytest.raises(InsufficientStock):
            services.write_off_damaged(product, 1, system_actor)

    def test_returns(self, product, system_actor):
        services.return_to_stock(product, 2, system_actor)
        services.return_to_stock(product, 1, system_actor, sellable=False)
        b = balance(product)
        assert (b.on_hand, b.damaged) == (2, 1)

    def test_transfer_between_locations(self, product, system_actor, shop):
        store = StockLocation.objects.create(code="store", name="Back store")
        services.receive(product, 10, system_actor, location=store)
        services.transfer(product, 4, system_actor, from_location=store, to_location=shop, idempotency_key="t1")
        services.transfer(product, 4, system_actor, from_location=store, to_location=shop, idempotency_key="t1")
        assert balance(product, store).on_hand == 6
        assert balance(product, shop).on_hand == 4
        assert services.availability(product).on_hand == 10
        with pytest.raises(ValidationError):
            services.transfer(product, 1, system_actor, from_location=shop, to_location=shop)

    @pytest.mark.parametrize("operation", ["update", "delete"])
    def test_ledger_is_append_only(self, product, system_actor, operation):
        movement = services.receive(product, 1, system_actor)
        with pytest.raises((InternalError, ProgrammingError)), transaction.atomic():
            qs = StockMovement.objects.filter(pk=movement.pk)
            qs.update(quantity=100) if operation == "update" else qs.delete()


class TestReservations:
    def test_reserve_release_consume(self, product, system_actor):
        services.receive(product, 10, system_actor)
        r1 = services.reserve(product, 4, system_actor, source=ORDER)
        r2 = services.reserve(product, 3, system_actor, source=ORDER)
        assert (balance(product).reserved, balance(product).available) == (7, 3)
        services.release(r1, system_actor)
        services.release(r1, system_actor)  # idempotent
        movement = services.consume(r2, system_actor)
        assert services.consume(r2, system_actor).pk == movement.pk  # idempotent
        b = balance(product)
        assert (b.on_hand, b.reserved) == (7, 0)
        assert movement.reason == MovementReason.SALE and movement.source_id == "o-1"
        r1.refresh_from_db()
        r2.refresh_from_db()
        assert (r1.status, r2.status) == (ReservationStatus.RELEASED, ReservationStatus.CONSUMED)

    def test_cannot_over_reserve(self, product, system_actor):
        services.receive(product, 2, system_actor)
        with pytest.raises(InsufficientStock) as exc:
            services.reserve(product, 3, system_actor, source=ORDER)
        assert exc.value.details["available"] == "2.000"

    def test_cannot_consume_released(self, product, system_actor):
        services.receive(product, 2, system_actor)
        reservation = services.reserve(product, 1, system_actor, source=ORDER)
        services.release(reservation, system_actor)
        with pytest.raises(Conflict):
            services.consume(reservation, system_actor)

    def test_reservation_idempotency(self, product, system_actor):
        services.receive(product, 5, system_actor)
        a = services.reserve(product, 2, system_actor, source=ORDER, idempotency_key="order-1-line-1")
        b = services.reserve(product, 2, system_actor, source=ORDER, idempotency_key="order-1-line-1")
        assert a.pk == b.pk and balance(product).reserved == 2

    def test_expired_reservations_are_released(self, product, system_actor):
        services.receive(product, 5, system_actor)
        past = timezone.now() - timedelta(minutes=1)
        services.reserve(product, 2, system_actor, source=ORDER, expires_at=past)
        services.reserve(product, 1, system_actor, source=ORDER, expires_at=timezone.now() + timedelta(hours=1))
        assert services.release_expired() == 1
        assert balance(product).reserved == 1
        from apps.inventory.tasks import release_expired_reservations

        assert release_expired_reservations.delay().get() == 0


class TestAdjustmentsAndCounts:
    def test_adjustment_requires_note_and_writes_ledger(self, product, system_actor, shop):
        with pytest.raises(ValidationError):
            services.record_adjustment(system_actor, product=product, location=shop, kind="FOUND", quantity=1, note="")
        adj = services.record_adjustment(
            system_actor, product=product, location=shop, kind=AdjustmentKind.OPENING_BALANCE, quantity=12,
            note="Initial count",
        )  # fmt: skip
        movement = StockMovement.objects.get(source_type="inventory.stockadjustment", source_id=str(adj.pk))
        assert movement.reason == MovementReason.OPENING_BALANCE and balance(product).on_hand == 12

    def test_count_sets_on_hand_and_records_variance(self, product, system_actor, shop):
        other = ProductFactory()
        services.receive(product, 10, system_actor)
        services.receive(other, 5, system_actor)
        count = services.start_count(shop, system_actor)
        assert count.lines.count() == 2
        count.lines.filter(product=product).update(counted_quantity=8)  # 2 missing
        count.lines.filter(product=other).update(counted_quantity=5)
        services.complete_count(count, system_actor)
        line = count.lines.get(product=product)
        assert (line.system_quantity, line.variance) == (10, -2)
        assert balance(product).on_hand == 8
        assert StockMovement.objects.filter(reason=MovementReason.STOCKTAKE_CORRECTION).count() == 1
        with pytest.raises(Conflict):
            services.complete_count(count, system_actor)

    def test_count_below_reserved_is_refused(self, product, system_actor, shop):
        services.receive(product, 10, system_actor)
        services.reserve(product, 5, system_actor, source=ORDER)
        count = services.start_count(shop, system_actor)
        count.lines.update(counted_quantity=3)
        with pytest.raises(Conflict, match="reserved"):
            services.complete_count(count, system_actor)
        assert balance(product).on_hand == 10

    def test_agents_cannot_complete_counts(self, product, shop):
        from apps.core.actors import Actor

        count = services.start_count(shop, Actor.system(), products=[product])
        with pytest.raises(Conflict):
            services.complete_count(count, Actor.agent("inventory"))


class TestChecks:
    def test_reconcile_clean_then_detects_tampering(self, product, system_actor):
        services.receive(product, 10, system_actor)
        services.reserve(product, 2, system_actor, source=ORDER)
        assert services.reconcile() == []
        StockBalance.objects.filter(product=product).update(on_hand=11)  # simulate a bug / manual SQL
        [mismatch] = services.reconcile()
        assert (mismatch["field"], mismatch["balance"], mismatch["expected"]) == ("on_hand", "11.000", "10.000")

    def test_low_stock(self, product, system_actor):
        services.receive(product, 10, system_actor)
        StockBalance.objects.filter(product=product).update(reorder_point=5)
        assert list(services.low_stock()) == []
        services.reserve(product, 5, system_actor, source=ORDER)
        assert [b.product for b in services.low_stock()] == [product]

    def test_availability_includes_incoming(self, product, system_actor):
        services.receive(product, 3, system_actor)
        a = services.availability(product)
        assert (a.on_hand, a.available, a.incoming) == (3, 3, 0)


def test_first_count_from_csv_rows(system_actor, shop):
    a, b = ProductFactory(sku="A-1"), ProductFactory(sku="B-2")
    ProductFactory(sku="C-3", status="ARCHIVED")
    count = services.start_count(shop, system_actor, products="all")
    assert count.lines.count() == 2  # archived excluded
    result = services.load_counts(count, [("a-1", "12"), ("B-2", "1.5"), ("NOPE", "3"), ("B-2", "x")], system_actor)
    assert result == {"loaded": 1, "unknown_skus": ["NOPE"], "invalid": ["B-2: '1.5'", "B-2: 'x'"]}
    services.complete_count(count, system_actor)
    assert balance(a).on_hand == 12
    assert not StockBalance.objects.filter(product=b).exclude(on_hand=0).exists()
