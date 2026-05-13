from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0030_seed_delegations"),
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
                    ("annulee", "Annulée"),
                    ("supprime_navex", "Supprimé Navex"),
                ],
                default="non_confirmee",
                max_length=30,
            ),
        ),
    ]
