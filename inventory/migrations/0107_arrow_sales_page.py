from django.db import migrations


def create_arrow_page(apps, schema_editor):
    """Create the 'Arrow.tn' sales page and point the public storefront
    (arrowsportwear.barats.tn) at it, so its orders are tagged Arrow.tn.
    Other pages (Barats.tn, Converty) are untouched. Offers are assigned to
    Arrow.tn by the owner in the offers manager."""
    SalesPage = apps.get_model("inventory", "SalesPage")
    AppKeyValue = apps.get_model("inventory", "AppKeyValue")
    arrow, _ = SalesPage.objects.get_or_create(
        name="Arrow.tn", defaults={"is_active": True})
    # storefront reads cfg:shop_sales_page_id (plain value, not a secret)
    AppKeyValue.objects.update_or_create(
        key="cfg:shop_sales_page_id", defaults={"value": str(arrow.id)})


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0106_customer_email"),
    ]

    operations = [
        migrations.RunPython(create_arrow_page, noop),
    ]
