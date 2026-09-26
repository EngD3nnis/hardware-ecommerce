import pytest
from django.db import InternalError, ProgrammingError, transaction

from apps.audit import services
from apps.audit.models import AuditEvent
from apps.core.actors import Actor
from apps.core.request_context import ClientMeta, set_client_meta, set_correlation_id

pytestmark = pytest.mark.django_db


def test_record_captures_actor_request_context_and_changes(staff_user):
    set_correlation_id("req-9")
    set_client_meta(ClientMeta(ip="10.0.0.5", user_agent="pytest"))
    try:
        event = services.record(
            Actor.for_user(staff_user), "thing.changed", staff_user, before={"a": 1}, after={"a": 2}, reason="because"
        )
    finally:
        set_correlation_id(None)
        set_client_meta(None)
    event.refresh_from_db()
    assert event.actor_type == "ADMIN"
    assert event.actor_user_id == str(staff_user.pk)
    assert event.object_type == "authentication.user"
    assert event.object_id == str(staff_user.pk)
    assert (event.before, event.after, event.reason) == ({"a": 1}, {"a": 2}, "because")
    assert (event.correlation_id, event.ip, event.user_agent) == ("req-9", "10.0.0.5", "pytest")


def test_agent_actor_records_run_id():
    event = services.record(Actor.agent("sales", run_id="run-1"), "quote.drafted", object_type="x", object_id="1")
    assert (event.actor_type, event.actor_label, event.agent_run_id) == ("AGENT", "sales", "run-1")


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_database_blocks_update_and_delete(operation):
    event = services.record(Actor.system(), "x.y", object_type="x", object_id="1")
    with pytest.raises((InternalError, ProgrammingError)), transaction.atomic():
        if operation == "update":
            AuditEvent.objects.filter(pk=event.pk).update(reason="tampered")
        else:
            AuditEvent.objects.filter(pk=event.pk).delete()
    event.refresh_from_db()
    assert event.reason == ""


def test_diff_and_snapshot(staff_user):
    snap = services.snapshot(staff_user, ["email", "is_staff"])
    assert snap == {"email": staff_user.email, "is_staff": True}
    assert services.diff({"a": 1, "b": 2}, {"a": 1, "b": 3}) == ({"b": 2}, {"b": 3})


def test_admin_is_read_only(client, superuser):
    services.record(Actor.system(), "x.y", object_type="x", object_id="1")
    client.force_login(superuser)
    assert client.get("/admin/audit/auditevent/").status_code == 200
    assert client.get("/admin/audit/auditevent/add/").status_code == 403
