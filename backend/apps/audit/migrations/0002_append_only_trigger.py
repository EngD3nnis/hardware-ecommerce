"""Make audit_auditevent append-only at the database level.

UPDATE and DELETE raise an error for every database user and code path
(admin, shell, raw SQL, bugs). TRUNCATE is deliberately not blocked, because
Django's test framework uses it to reset tables; production roles should not
have TRUNCATE on this table (see docs/architecture/security.md).
"""

from django.db import migrations

FORWARD = """
CREATE FUNCTION audit_event_is_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_auditevent is append-only (% blocked)', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_event_no_update_or_delete
    BEFORE UPDATE OR DELETE ON audit_auditevent
    FOR EACH ROW EXECUTE FUNCTION audit_event_is_append_only();
"""

REVERSE = """
DROP TRIGGER IF EXISTS audit_event_no_update_or_delete ON audit_auditevent;
DROP FUNCTION IF EXISTS audit_event_is_append_only();
"""


class Migration(migrations.Migration):
    dependencies = [("audit", "0001_initial")]

    operations = [migrations.RunSQL(FORWARD, REVERSE)]
