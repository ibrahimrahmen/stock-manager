from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0064_ad_campaign_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="ad",
            name="spend_original",
            field=models.DecimalField(
                max_digits=12, decimal_places=2, default=0,
                help_text="Depense dans la devise d'origine du compte.",
            ),
        ),
        migrations.AddField(
            model_name="ad",
            name="account_id",
            field=models.CharField(
                max_length=64, blank=True, default="",
                help_text="Compte publicitaire Meta d'ou vient cette pub.",
            ),
        ),
        migrations.AddField(
            model_name="ad",
            name="currency",
            field=models.CharField(
                max_length=8, blank=True, default="",
                help_text="Devise d'origine du compte (EUR, USD, TND...).",
            ),
        ),
    ]
