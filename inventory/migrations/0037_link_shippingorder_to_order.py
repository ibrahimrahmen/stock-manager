from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0036_exchangereturnitem"),
    ]

    operations = [
        # Index rename from the previous makemigrations dry-run output (Django 6.0 quirk)
        migrations.RenameIndex(
            model_name="exchangereturnitem",
            new_name="inventory_e_exchang_04b9e9_idx",
            old_name="inventory_e_exchang_a7e8c6_idx",
        ),
        # The actual new field for linking v1 ShippingOrder to v2 Order
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
