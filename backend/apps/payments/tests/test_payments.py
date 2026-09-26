import json
from decimal import Decimal
from unittest import mock

import pytest
from django.contrib.auth.models import Permission

from apps.catalog.tests.factories import ProductFactory
from apps.core.actors import Actor
from apps.core.exceptions import Conflict, PermissionDenied, ValidationError
from apps.customers import services as customers
from apps.inventory import services as inventory
from apps.payments import mpesa, services
from apps.payments.models import EventStatus, InboundPaymentEvent, Payment, PaymentState
from apps.pricing import services as pricing
from apps.sales import services as sales
from apps.sales.models import PaymentStatus

pytestmark = pytest.mark.django_db
TOKEN = "cb-secret-token-123"


@pytest.fixture(autouse=True)
def mpesa_settings(settings):
    settings.MPESA_CONSUMER_KEY = "key"
    settings.MPESA_CONSUMER_SECRET = "secret"
    settings.MPESA_SHORTCODE = "174379"
    settings.MPESA_PASSKEY = "passkey"
    settings.MPESA_CALLBACK_BASE_URL = "https://api.example.test"
    settings.MPESA_CALLBACK_TOKEN = TOKEN


@pytest.fixture
def staff(staff_user):
    return Actor.for_user(staff_user)


@pytest.fixture
def order(staff):
    product = ProductFactory(price_on_request=False)
    pricing.set_price(product, "1000", staff)
    inventory.receive(product, 5, staff)
    customer = customers.create_customer(staff, name="Otieno", phone="0722000111")
    order = sales.create_order(staff, customer=customer, lines=[{"product": product, "quantity": 2}])
    return sales.confirm_order(order, staff)


def callback(checkout_id, code=0, amount=2000, receipt="QKX123ABC"):
    body = {
        "Body": {
            "stkCallback": {
                "MerchantRequestID": "m1",
                "CheckoutRequestID": checkout_id,
                "ResultCode": code,
                "ResultDesc": "ok" if code == 0 else "Request cancelled by user",
            }
        }
    }
    if code == 0:
        body["Body"]["stkCallback"]["CallbackMetadata"] = {
            "Item": [
                {"Name": "Amount", "Value": amount},
                {"Name": "MpesaReceiptNumber", "Value": receipt},
                {"Name": "TransactionDate", "Value": 20260926101500},
                {"Name": "PhoneNumber", "Value": 254722000111},
            ]
        }
    return body


def fake_daraja(checkout_id="ws_CO_1"):
    token = mock.Mock(status_code=200, json=lambda: {"access_token": "t"})
    token.raise_for_status = lambda: None
    push = mock.Mock(
        status_code=200,
        json=lambda: {
            "ResponseCode": "0",
            "CheckoutRequestID": checkout_id,
            "MerchantRequestID": "m1",
            "CustomerMessage": "ok",
        },
    )
    fakes = {"get": mock.Mock(return_value=token), "post": mock.Mock(return_value=push)}
    patcher = mock.patch.multiple("apps.payments.mpesa.requests", **fakes)

    class _Ctx:
        def __enter__(self):
            patcher.start()
            return fakes

        def __exit__(self, *exc):
            patcher.stop()

    return _Ctx()


def test_manual_payment_updates_order(order, staff):
    services.record_payment(order, staff, method="CASH", amount="500")
    order.refresh_from_db()
    assert (order.payment_status, order.amount_paid, order.balance_due) == ("PARTIALLY_PAID", 500, 1500)
    services.record_payment(order, staff, method="MPESA", amount="1500", reference="qkx999")
    order.refresh_from_db()
    assert order.payment_status == PaymentStatus.PAID


def test_same_reference_cannot_be_recorded_twice(order, staff):
    services.record_payment(order, staff, method="MPESA", amount="100", reference="ABC")
    with pytest.raises(Conflict):
        services.record_payment(order, staff, method="MPESA", amount="100", reference="abc")
    with pytest.raises(ValidationError):
        services.record_payment(order, staff, method="MPESA", amount="100", reference="")


def test_agents_cannot_record_payments_or_refunds(order, staff):
    agent = Actor.agent("ops")
    with pytest.raises(PermissionDenied):
        services.record_payment(order, agent, method="CASH", amount="1")
    payment = services.record_payment(order, staff, method="CASH", amount="100")
    with pytest.raises(PermissionDenied):
        services.record_refund(payment, agent, amount="1", reason="x")


def test_stk_push_then_success_callback(order, staff, client, django_capture_on_commit_callbacks):
    with fake_daraja() as fakes:
        payment = services.request_mpesa_payment(order, staff, phone="0722000111")
    sent = fakes["post"].call_args.kwargs["json"]
    assert sent["Amount"] == 2000 and sent["PhoneNumber"] == "254722000111"
    assert sent["CallBackURL"] == f"https://api.example.test/webhooks/mpesa/{TOKEN}/"
    order.refresh_from_db()
    assert order.payment_status == PaymentStatus.PENDING

    url = f"/webhooks/mpesa/{TOKEN}/"
    body = json.dumps(callback("ws_CO_1"))
    for _ in range(3):  # Safaricom retries; duplicates must be harmless
        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(url, body, content_type="application/json")
        assert response.status_code == 200 and response.json()["ResultCode"] == 0
    payment.refresh_from_db()
    order.refresh_from_db()
    assert payment.state == PaymentState.COMPLETED and payment.provider_reference == "QKX123ABC"
    assert order.payment_status == PaymentStatus.PAID and order.amount_paid == Decimal("2000")
    assert Payment.objects.count() == 1 and InboundPaymentEvent.objects.count() == 1


def test_failed_callback_marks_payment_failed(order, staff):
    with fake_daraja():
        payment = services.request_mpesa_payment(order, staff, phone="0722000111")
    event, _ = services.store_mpesa_callback(callback("ws_CO_1", code=1032), "1.2.3.4")
    services.process_mpesa_event(event)
    payment.refresh_from_db()
    order.refresh_from_db()
    assert payment.state == PaymentState.FAILED and order.payment_status == PaymentStatus.UNPAID


def test_amount_mismatch_is_not_completed(order, staff):
    with fake_daraja():
        payment = services.request_mpesa_payment(order, staff, phone="0722000111")
    event, _ = services.store_mpesa_callback(callback("ws_CO_1", amount=1), None)
    event = services.process_mpesa_event(event)
    payment.refresh_from_db()
    assert event.status == EventStatus.FAILED and "mismatch" in event.error
    assert payment.state == PaymentState.PENDING


def test_spoofed_callbacks_are_rejected(order, staff, client, settings, django_capture_on_commit_callbacks):
    url_bad = "/webhooks/mpesa/wrong-token/"
    assert client.post(url_bad, json.dumps(callback("x")), content_type="application/json").status_code == 404
    # Correct token but a CheckoutRequestID we never issued: stored, ignored, no payment.
    with django_capture_on_commit_callbacks(execute=True):
        client.post(f"/webhooks/mpesa/{TOKEN}/", json.dumps(callback("forged")), content_type="application/json")
    assert InboundPaymentEvent.objects.get().status == EventStatus.IGNORED
    assert not Payment.objects.exists()
    settings.MPESA_CALLBACK_ALLOWED_IPS = ["196.201.214.200"]
    response = client.post(f"/webhooks/mpesa/{TOKEN}/", "{}", content_type="application/json")
    assert response.status_code == 404
    assert client.get(f"/webhooks/mpesa/{TOKEN}/").status_code == 405


def test_daraja_errors_become_domain_errors(order, staff):
    with mock.patch("apps.payments.mpesa.requests.get", side_effect=mpesa.requests.ConnectionError):
        with pytest.raises(mpesa.MpesaError):
            services.request_mpesa_payment(order, staff, phone="0722000111")
    assert not Payment.objects.exists()


def test_cancelled_order_cannot_keep_money(order, staff, staff_user):
    payment = services.record_payment(order, staff, method="CASH", amount="2000")
    with pytest.raises(Conflict, match="Record the refund first"):
        sales.cancel_order(order, staff, "customer cancelled")
    with pytest.raises(PermissionDenied):
        services.record_refund(payment, staff, amount="2000", reason="cancelled", user=staff_user)
    staff_user.user_permissions.add(Permission.objects.get(codename="record_refund"))
    staff_user = type(staff_user).objects.get(pk=staff_user.pk)  # reset permission cache
    with pytest.raises(ValidationError):
        services.record_refund(payment, staff, amount="2001", reason="too much", user=staff_user)
    services.record_refund(payment, staff, amount="2000", reason="cancelled", user=staff_user)
    order.refresh_from_db()
    assert order.payment_status == PaymentStatus.REFUNDED
    sales.cancel_order(order, staff, "customer cancelled")
    order.refresh_from_db()
    assert order.status == "CANCELLED"
