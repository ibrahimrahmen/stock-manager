from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0063_ad_attribution_offers"),
    ]

    operations = [
        migrations.AddField(
            model_name="ad",
            name="campaign_id",
            field=models.CharField(
                blank=True, null=True, unique=True, max_length=64,
                help_text="ID de campagne Meta (cle stable de synchronisation).",
            ),
        ),
        migrations.AlterField(
            model_name="ad",
            name="campaign_name",
            field=models.CharField(
                db_index=True, max_length=200,
                help_text="Nom de la campagne (affichage ; peut changer).",
            ),
        ),
    ]
