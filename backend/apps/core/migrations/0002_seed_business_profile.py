"""Create the single BusinessProfile row with the details from the static website."""

from django.db import migrations


def create_profile(apps, schema_editor):
    BusinessProfile = apps.get_model("core", "BusinessProfile")
    BusinessProfile.objects.get_or_create(
        id=1,
        defaults={
            "business_name": "Dewmix Hardware",
            "whatsapp_sales_number": "254787151516",
            "other_phone_numbers": ["254743448862"],
            "email": "sales@dewmix.com",
            "website_url": "https://dewmixhardware.com",
            "address": "Kenol, Murang'a (along the Nyeri–Nairobi Highway), Kenya",
            "map_url": "https://maps.app.goo.gl/F4ym5pFBhR5vY2zT9",
            "opening_hours": ["Mo-Fr 07:00-18:00", "Sa 07:00-17:00", "Su 09:00-14:00"],
            "show_prices_online": False,
            "quote_validity_days": 14,
        },
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial")]

    operations = [migrations.RunPython(create_profile, migrations.RunPython.noop)]
