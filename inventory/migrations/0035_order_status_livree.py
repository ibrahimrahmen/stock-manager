from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0034_order_exchange_fields"),
    ]

    operations = [
        migrations.AlterField(
            model_name="order",
            name="status",
            field=models.CharField(
                choices=[
                    ("non_confirmee", "Non confirmée"),
                    ("confirmee", "Confirmée"),
                    ("injoignable", "Injoignable"),
                    ("pas_serieux", "Pas sérieux"),
                    ("rappeler_plus_tard", "Rappeler plus tard"),
                    ("livree", "Livrée"),
                    ("annulee", "Annulée"),
                    ("supprime_navex", "Supprimé Navex"),
                ],
                default="non_confirmee",
                max_length=30,
            ),
        ),
    ]
