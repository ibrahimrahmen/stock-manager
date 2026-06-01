from django.db import migrations


def add_region_as_delegation(apps, schema_editor):
    """For every region (governorate), make sure a delegation with the SAME
    name exists under it, so the city dropdown offers e.g. "Le Kef" itself —
    not only "Le Kef Est" / "Le Kef Ouest". Idempotent: skips ones already there.
    """
    Region = apps.get_model("inventory", "Region")
    Delegation = apps.get_model("inventory", "Delegation")
    for region in Region.objects.all():
        Delegation.objects.get_or_create(
            region=region,
            name=region.name,
            defaults={"is_active": True},
        )


def remove_region_self_delegation(apps, schema_editor):
    """Reverse: delete delegations whose name equals their region's name."""
    Region = apps.get_model("inventory", "Region")
    Delegation = apps.get_model("inventory", "Delegation")
    for region in Region.objects.all():
        Delegation.objects.filter(region=region, name=region.name).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0042_ad"),
    ]

    operations = [
        migrations.RunPython(add_region_as_delegation, remove_region_self_delegation),
    ]
