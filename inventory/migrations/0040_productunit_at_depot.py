from django.db import migrations, models


class Migration(migrations.Migration):
    """Add AT_DEPOT status to ProductUnit and StockMovement.

    Safe: just enlarges the choices list, no DB schema change beyond enum.
    """

    dependencies = [
        ("inventory", "0039_fix_exchangereturnitem_state"),
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
                    ("at_depot", "Retour en dépôt Navex"),
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
                    ("at_depot", "Retour en dépôt Navex"),
                ],
                max_length=20,
            ),
        ),
    ]
