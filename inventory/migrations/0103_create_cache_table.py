from django.core.management import call_command
from django.db import migrations


def create_cache_table(apps, schema_editor):
    # Creates the DatabaseCache table(s) declared in settings.CACHES.
    # Idempotent: skips tables that already exist.
    call_command("createcachetable")


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0102_product_source_test"),
    ]

    operations = [
        migrations.RunPython(create_cache_table, migrations.RunPython.noop),
    ]
