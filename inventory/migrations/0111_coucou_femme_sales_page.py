from django.db import migrations


def ensure_coucou_femme(apps, schema_editor):
    """Ensure the 'Coucou femme' SalesPage exists at id 13, matching the
    MESSENGER_PAGE_TO_SALESPAGE / _REPLY_PAGES entries in views.py that route
    Facebook page 938553302679435 to it. Idempotent: on production the row was
    already created, so this just confirms it; on a fresh DB it creates it."""
    SalesPage = apps.get_model("inventory", "SalesPage")
    SalesPage.objects.update_or_create(
        id=13, defaults={"name": "Coucou femme", "is_active": True})


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0110_split_multi_offers"),
    ]

    operations = [
        migrations.RunPython(ensure_coucou_femme, noop),
    ]
