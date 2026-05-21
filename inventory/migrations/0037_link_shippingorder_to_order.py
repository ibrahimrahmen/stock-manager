from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0036_exchangereturnitem"),
    ]

    operations = [
        # Just add the new FK linking v1 ShippingOrder to v2 Order.
        # (We skip the auto-detected RenameIndex from Django 6.0 — the actual
        # index name in Postgres doesn't match what Django expects, so renaming
        # crashes. It's a purely cosmetic change anyway.)
        migrations.AddField(
            model_name="shippingorder",
            name="order",
            field=models.ForeignKey(
                blank=True,
                help_text="Order v2 lié à ce ShippingOrder, si applicable.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="shipping_orders",
                to="inventory.order",
            ),
        ),
    ]
