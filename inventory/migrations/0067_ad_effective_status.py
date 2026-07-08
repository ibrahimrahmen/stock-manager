from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0066_ad_archived"),
    ]

    operations = [
        migrations.AddField(
            model_name="ad",
            name="effective_status",
            field=models.CharField(
                max_length=32, blank=True, default="",
                help_text="Statut Meta de la campagne (ACTIVE, PAUSED, DELETED...), rafraichi a chaque sync.",
            ),
        ),
    ]
