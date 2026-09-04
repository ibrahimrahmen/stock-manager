"""Seed a fresh Unifunl heartbeat on deploy.

Failover treats a missing/stale heartbeat as 'Unifunl down' and lets our own DM
automation take over. Without seeding, the moment this code deploys (before the
first cron sync) the heartbeat would be missing, so our system would briefly
answer Barats DMs alongside Unifunl (double replies). Seeding a fresh stamp
gives Unifunl its normal grace window at deploy time; the cron keeps it fresh,
and it only goes stale if Unifunl genuinely stops responding.
"""
from django.db import migrations


def seed_heartbeat(apps, schema_editor):
    AppKeyValue = apps.get_model("inventory", "AppKeyValue")
    # auto_now on updated_at stamps 'now' on save.
    AppKeyValue.objects.update_or_create(
        key="unifunl_last_ok", defaults={"value": "seed"})


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0076_appkeyvalue"),
    ]

    operations = [
        migrations.RunPython(seed_heartbeat, noop_reverse),
    ]
