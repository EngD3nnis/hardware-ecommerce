"""Business-local time. Storage is UTC; days, years and cut-offs are Nairobi time."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone


def business_tz() -> ZoneInfo:
    return ZoneInfo(settings.BUSINESS_TIMEZONE)


def business_now() -> datetime:
    return timezone.now().astimezone(business_tz())


def business_today() -> date:
    return business_now().date()
