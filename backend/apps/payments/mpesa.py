"""Safaricom Daraja (M-Pesa) client: STK push ("Lipa na M-Pesa Online").

The integration boundary: nothing outside this module knows Daraja's URLs or
field names. Configure with MPESA_* settings (see .env.example). Sandbox by default.

Daraja callbacks are not signed, so they are authenticated by:
- a secret token in the callback URL path (MPESA_CALLBACK_TOKEN);
- optionally, a source-IP allow-list (MPESA_CALLBACK_ALLOWED_IPS);
- and, most importantly, the callback can only complete a payment we
  initiated (its CheckoutRequestID must match), for the amount we requested.
"""

import base64
from dataclasses import dataclass
from datetime import datetime

import requests
from django.conf import settings
from django.core.cache import cache

from apps.core.exceptions import DomainError
from apps.core.timeutils import business_now

BASE_URLS = {"sandbox": "https://sandbox.safaricom.co.ke", "production": "https://api.safaricom.co.ke"}
TIMEOUT = 15


class MpesaError(DomainError):
    code = "mpesa_error"
    http_status = 502
    default_message = "M-Pesa request failed. Try again or take payment another way."


@dataclass(frozen=True)
class StkPushResult:
    checkout_request_id: str
    merchant_request_id: str
    customer_message: str


@dataclass(frozen=True)
class StkCallback:
    checkout_request_id: str
    result_code: int
    result_description: str
    amount: str | None = None
    receipt_number: str | None = None
    phone: str | None = None
    transaction_date: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.result_code == 0


def _base_url() -> str:
    return BASE_URLS[settings.MPESA_ENVIRONMENT]


def is_configured() -> bool:
    return all(
        [
            settings.MPESA_CONSUMER_KEY,
            settings.MPESA_CONSUMER_SECRET,
            settings.MPESA_SHORTCODE,
            settings.MPESA_PASSKEY,
            settings.MPESA_CALLBACK_BASE_URL,
            settings.MPESA_CALLBACK_TOKEN,
        ]
    )


def _access_token() -> str:
    cached = cache.get("mpesa:access_token")
    if cached:
        return cached
    try:
        response = requests.get(
            f"{_base_url()}/oauth/v1/generate?grant_type=client_credentials",
            auth=(settings.MPESA_CONSUMER_KEY, settings.MPESA_CONSUMER_SECRET),
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        token = response.json()["access_token"]
    except (requests.RequestException, KeyError, ValueError) as exc:
        raise MpesaError("Could not authenticate with M-Pesa.") from exc
    cache.set("mpesa:access_token", token, timeout=50 * 60)  # Daraja tokens last 60 minutes
    return token


def stk_password(timestamp: str) -> str:
    raw = f"{settings.MPESA_SHORTCODE}{settings.MPESA_PASSKEY}{timestamp}"
    return base64.b64encode(raw.encode()).decode()


def callback_url() -> str:
    base = settings.MPESA_CALLBACK_BASE_URL.rstrip("/")
    return f"{base}/webhooks/mpesa/{settings.MPESA_CALLBACK_TOKEN}/"


def stk_push(*, phone: str, amount: int, account_reference: str, description: str) -> StkPushResult:
    """Ask the customer's phone to approve a payment. Amount is whole shillings (Daraja requirement)."""
    if not is_configured():
        raise MpesaError("M-Pesa is not configured on this server.")
    timestamp = business_now().strftime("%Y%m%d%H%M%S")
    payload = {
        "BusinessShortCode": settings.MPESA_SHORTCODE,
        "Password": stk_password(timestamp),
        "Timestamp": timestamp,
        "TransactionType": settings.MPESA_TRANSACTION_TYPE,
        "Amount": amount,
        "PartyA": phone,
        "PartyB": settings.MPESA_SHORTCODE,
        "PhoneNumber": phone,
        "CallBackURL": callback_url(),
        "AccountReference": account_reference[:12],
        "TransactionDesc": description[:13],
    }
    try:
        response = requests.post(
            f"{_base_url()}/mpesa/stkpush/v1/processrequest",
            json=payload,
            headers={"Authorization": f"Bearer {_access_token()}"},
            timeout=TIMEOUT,
        )
        body = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise MpesaError() from exc
    if response.status_code != 200 or str(body.get("ResponseCode")) != "0":
        raise MpesaError(body.get("errorMessage") or body.get("ResponseDescription") or "M-Pesa rejected the request.")
    return StkPushResult(
        checkout_request_id=body["CheckoutRequestID"],
        merchant_request_id=body.get("MerchantRequestID", ""),
        customer_message=body.get("CustomerMessage", ""),
    )


def parse_stk_callback(payload: dict) -> StkCallback:
    """Parse Daraja's STK callback body. Raises ValueError if it is not one."""
    callback = payload["Body"]["stkCallback"]
    items = {i["Name"]: i.get("Value") for i in callback.get("CallbackMetadata", {}).get("Item", [])}
    return StkCallback(
        checkout_request_id=str(callback["CheckoutRequestID"]),
        result_code=int(callback["ResultCode"]),
        result_description=str(callback.get("ResultDesc", "")),
        amount=str(items["Amount"]) if "Amount" in items else None,
        receipt_number=items.get("MpesaReceiptNumber"),
        phone=str(items["PhoneNumber"]) if "PhoneNumber" in items else None,
        transaction_date=str(items["TransactionDate"]) if "TransactionDate" in items else None,
    )


def parse_transaction_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=business_now().tzinfo)
    except ValueError:
        return None
