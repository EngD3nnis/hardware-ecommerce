from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.catalog.tests.factories import ProductFactory
from apps.core.exceptions import Conflict, ValidationError
from apps.pricing import services
from apps.pricing.models import PriceList, ProductPrice

pytestmark = pytest.mark.django_db


@pytest.fixture
def product():
    return ProductFactory(price_on_request=False)


def test_default_price_list_is_seeded():
    retail = services.default_price_list()
    assert (retail.code, retail.currency, retail.prices_include_vat) == ("retail", "KES", True)


def test_no_price_means_on_request(product):
    quote = services.quote_price(product)
    assert quote.on_request and quote.amount is None
    assert quote.display == "Price on request"


def test_set_price_closes_previous_and_keeps_history(product, system_actor):
    first = services.set_price(product, "1,200", system_actor)
    later = timezone.now() + timedelta(seconds=1)
    second = services.set_price(product, "1250.50", system_actor, effective_from=later)
    first.refresh_from_db()
    assert first.valid_to == later
    assert second.valid_to is None
    assert services.current_price(product, at=later - timedelta(microseconds=1)).amount == Decimal("1200")
    assert services.current_price(product, at=later).amount == Decimal("1250.50")
    assert services.quote_price(product, at=later).display == "KES 1,250.50"
    assert AuditEvent.objects.filter(action="pricing.price.set").count() == 2


def test_same_price_is_a_no_op(product, system_actor):
    services.set_price(product, "100", system_actor)
    assert services.set_price(product, "100.00", system_actor) is None
    assert ProductPrice.objects.count() == 1


def test_price_on_request_flag_overrides_list_price(product, system_actor):
    services.set_price(product, "100", system_actor)
    product.price_on_request = True
    assert services.quote_price(product).on_request


@pytest.mark.parametrize("amount", ["0", "-5", "abc", "1.001", "NaN", "100000000"])
def test_rejects_bad_amounts(product, system_actor, amount):
    with pytest.raises(ValidationError):
        services.set_price(product, amount, system_actor)


def test_back_dating_is_refused(product, system_actor):
    services.set_price(product, "100", system_actor)
    with pytest.raises(Conflict):
        services.set_price(product, "90", system_actor, effective_from=timezone.now() - timedelta(days=1))


def test_end_price_makes_it_on_request(product, system_actor):
    services.set_price(product, "100", system_actor, effective_from=timezone.now() - timedelta(minutes=1))
    assert services.end_price(product, system_actor) is True
    assert services.quote_price(product).on_request
    assert services.end_price(product, system_actor) is False


def test_database_rejects_overlapping_periods(product):
    retail = PriceList.objects.get(code="retail")
    now = timezone.now()
    ProductPrice.objects.create(product=product, price_list=retail, amount=10, valid_from=now)
    with pytest.raises(IntegrityError), transaction.atomic():
        ProductPrice.objects.create(product=product, price_list=retail, amount=11, valid_from=now + timedelta(days=1))


def test_database_rejects_non_positive_amount(product):
    retail = PriceList.objects.get(code="retail")
    with pytest.raises(IntegrityError), transaction.atomic():
        ProductPrice.objects.create(product=product, price_list=retail, amount=0, valid_from=timezone.now())


def test_only_one_default_price_list():
    with pytest.raises(IntegrityError), transaction.atomic():
        PriceList.objects.create(code="trade", name="Trade", is_default=True)
