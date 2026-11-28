"""Deactivate the AI-office employees (Sami/finance, Nour/marketing).

The daily Railway cron was running both every morning via the Claude API,
costing ~$10/day. The two crons were removed from railway.toml; this also
flips every AIEmployee to is_active=False so the daily cron skips them even
if the endpoint is ever hit. Reversible (sets them active again).

The owner can re-activate an employee from the office UI whenever wanted.
"""
from django.db import migrations


def _deactivate(apps, schema_editor):
    AIEmployee = apps.get_model("inventory", "AIEmployee")
    AIEmployee.objects.update(is_active=False)


def _reactivate(apps, schema_editor):
    AIEmployee = apps.get_model("inventory", "AIEmployee")
    AIEmployee.objects.update(is_active=True)


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0115_ai_conversation"),
    ]

    operations = [
        migrations.RunPython(_deactivate, _reactivate),
    ]
