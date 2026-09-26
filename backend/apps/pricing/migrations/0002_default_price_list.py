from django.db import migrations


def create_retail(apps, schema_editor):
    PriceList = apps.get_model("pricing", "PriceList")
    if not PriceList.objects.filter(is_default=True).exists():
        PriceList.objects.get_or_create(
            code="retail",
            defaults={"name": "Retail", "currency": "KES", "is_default": True, "prices_include_vat": True},
        )


class Migration(migrations.Migration):
    dependencies = [("pricing", "0001_initial")]

    operations = [migrations.RunPython(create_retail, migrations.RunPython.noop)]
