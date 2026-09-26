"""Stock ledger is append-only; seed the default stock location."""

from django.db import migrations

FORWARD = """
CREATE FUNCTION stock_movement_is_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'inventory_stockmovement is append-only (% blocked); correct stock with a new movement', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER stock_movement_no_update_or_delete
    BEFORE UPDATE OR DELETE ON inventory_stockmovement
    FOR EACH ROW EXECUTE FUNCTION stock_movement_is_append_only();
"""

REVERSE = """
DROP TRIGGER IF EXISTS stock_movement_no_update_or_delete ON inventory_stockmovement;
DROP FUNCTION IF EXISTS stock_movement_is_append_only();
"""


def create_main_location(apps, schema_editor):
    StockLocation = apps.get_model("inventory", "StockLocation")
    if not StockLocation.objects.filter(is_default=True).exists():
        StockLocation.objects.get_or_create(code="kenol", defaults={"name": "Kenol shop", "is_default": True})


class Migration(migrations.Migration):
    dependencies = [("inventory", "0001_initial")]

    operations = [
        migrations.RunSQL(FORWARD, REVERSE),
        migrations.RunPython(create_main_location, migrations.RunPython.noop),
    ]
