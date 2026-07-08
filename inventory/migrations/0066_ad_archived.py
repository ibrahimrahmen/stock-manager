from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0065_ad_currency_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="ad",
            name="archived",
            field=models.BooleanField(
                default=False,
                help_text="Pub annulee/desactivee dans Meta : masquee du dashboard du jour et exclue de l'attribution (l'historique passe reste).",
            ),
        ),
    ]
