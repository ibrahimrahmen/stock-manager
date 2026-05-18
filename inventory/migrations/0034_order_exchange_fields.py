from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0033_product_parent_product"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="exchange_of",
            field=models.ForeignKey(
                blank=True,
                help_text="Commande livrée d'origine, si celle-ci est un échange.",
                null=True,
                on_delete=models.deletion.SET_NULL,
                related_name="exchanges",
                to="inventory.order",
            ),
        ),
        migrations.AddField(
            model_name="order",
            name="navex_return_barcode",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Barcode du colis de retour (généré par Navex pour les échanges).",
                max_length=80,
            ),
        ),
        migrations.AddField(
            model_name="order",
            name="exchange_fault",
            field=models.CharField(
                choices=[
                    ("none", "—"),
                    ("ours", "Notre faute"),
                    ("client", "Faute client"),
                ],
                default="none",
                help_text="Pour les échanges : qui est en faute. Affecte les frais de livraison.",
                max_length=10,
            ),
        ),
    ]
