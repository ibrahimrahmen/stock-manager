from django.db import migrations


def set_pixel(apps, schema_editor):
    """Set the storefront Meta Pixel ID. Public identifier (appears in page
    source); the owner can change it in the Configuration Center."""
    AppKeyValue = apps.get_model("inventory", "AppKeyValue")
    AppKeyValue.objects.update_or_create(
        key="cfg:shop_pixel_id", defaults={"value": "1683528096738639"})


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0107_arrow_sales_page"),
    ]

    operations = [
        migrations.RunPython(set_pixel, noop),
    ]
