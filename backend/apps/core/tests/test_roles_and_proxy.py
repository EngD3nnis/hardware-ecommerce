import io

import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import RequestFactory

from apps.core.middleware import client_ip

pytestmark = pytest.mark.django_db


def test_setup_roles_is_idempotent():
    call_command("setup_roles", stdout=io.StringIO())
    call_command("setup_roles", stdout=io.StringIO())
    assert set(Group.objects.values_list("name", flat=True)) == {"Owner", "Shop attendant", "Storekeeper", "Accounts"}
    accounts = Group.objects.get(name="Accounts")
    assert accounts.permissions.filter(codename="record_refund").exists()
    assert not Group.objects.get(name="Shop attendant").permissions.filter(codename="record_refund").exists()


def test_forwarded_for_trusted_only_from_proxy(settings):
    rf = RequestFactory()
    settings.TRUSTED_PROXY_IPS = ["172.18.0.2"]
    spoofed = rf.get("/", REMOTE_ADDR="41.90.1.1", HTTP_X_FORWARDED_FOR="1.2.3.4")
    assert client_ip(spoofed) == "41.90.1.1"
    via_proxy = rf.get("/", REMOTE_ADDR="172.18.0.2", HTTP_X_FORWARDED_FOR="6.6.6.6, 41.90.1.1")
    assert client_ip(via_proxy) == "41.90.1.1"
