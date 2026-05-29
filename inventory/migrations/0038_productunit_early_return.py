from django.db import migrations, models


class Migration(migrations.Migration):
    """Add EARLY_RETURN status to ProductUnit and StockMovement.

    Safe migration:
    - Adds new choice values only; no schema change beyond enum-like enlargement.
    - No data modification.
    - No risk to existing rows: the new value is opt-in.
    """

    dependencies = [
        ("inventory", "0037_link_shippingorder_to_order"),
    ]

    operations = [
        migrations.AlterField(
            model_name="productunit",
            name="status",
            field=models.CharField(
                choices=[
                    ("in_stock", "En stock"),
                    ("shipped", "Expédié"),
                    ("paid", "Payé"),
                    ("returned", "Retourné"),
                    ("early_return", "Retour anticipé"),
                ],
                default="in_stock",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="stockmovement",
            name="movement_type",
            field=models.CharField(
                choices=[
                    ("received", "Réception"),
                    ("shipped", "Expédition"),
                    ("paid", "Payé"),
                    ("returned", "Retour"),
                    ("early_return", "Retour anticipé"),
                ],
                max_length=20,
            ),
        ),
    ]
