from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations

from apps.search.services import DEFAULT_SYNONYMS


def seed(apps, schema_editor):
    Synonym = apps.get_model("search", "Synonym")
    for terms in DEFAULT_SYNONYMS:
        Synonym.objects.get_or_create(terms=terms)


class Migration(migrations.Migration):
    dependencies = [("search", "0001_synonyms"), ("catalog", "0001_initial")]

    operations = [
        TrigramExtension(),
        migrations.RunSQL(
            "CREATE INDEX IF NOT EXISTS catalog_product_name_trgm ON catalog_product USING gin (name gin_trgm_ops);",
            "DROP INDEX IF EXISTS catalog_product_name_trgm;",
        ),
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
