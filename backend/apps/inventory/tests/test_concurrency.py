"""Real concurrent transactions against PostgreSQL: row locks must serialise stock changes."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import connection

from apps.catalog.tests.factories import ProductFactory
from apps.core.actors import Actor
from apps.inventory import services
from apps.inventory.models import Reservation, StockBalance
from apps.inventory.services import InsufficientStock, Source

# Real transactions (no test-wide rollback). serialized_rollback restores the rows seeded by
# migrations (default location, price list, business profile) that the table flush removes.
pytestmark = pytest.mark.django_db(transaction=True, serialized_rollback=True)


def _run_concurrently(fn, n):
    barrier = threading.Barrier(n)

    def worker(i):
        try:
            barrier.wait()
            return fn(i)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(worker, range(n)))


def test_only_one_of_many_can_reserve_the_last_unit():
    actor = Actor.system("test")
    product = ProductFactory()
    services.receive(product, 1, actor)

    def attempt(i):
        try:
            services.reserve(product, 1, actor, source=Source("orders.order", str(i)))
            return "ok"
        except InsufficientStock:
            return "refused"

    results = _run_concurrently(attempt, 8)
    assert sorted(results) == ["ok"] + ["refused"] * 7
    balance = StockBalance.objects.get(product=product)
    assert (balance.on_hand, balance.reserved) == (1, 1)
    assert Reservation.objects.count() == 1


def test_concurrent_receipts_all_count():
    actor = Actor.system("test")
    product = ProductFactory()
    _run_concurrently(lambda i: services.receive(product, 2, actor), 6)
    assert StockBalance.objects.get(product=product).on_hand == 12
    assert services.reconcile() == []


def test_concurrent_duplicate_idempotency_key_moves_stock_once():
    actor = Actor.system("test")
    product = ProductFactory()

    def attempt(i):
        try:
            services.receive(product, 5, actor, idempotency_key="same-webhook")
            return "ok"
        except Exception as exc:  # noqa: BLE001 - any failure is acceptable here, double-counting is not
            return type(exc).__name__

    _run_concurrently(attempt, 5)
    assert StockBalance.objects.get(product=product).on_hand == 5
