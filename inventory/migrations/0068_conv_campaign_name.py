from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0067_ad_effective_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="messengerconversation",
            name="source_campaign_name",
            field=models.CharField(
                max_length=200, blank=True, default="",
                help_text="Vrai nom de la campagne Meta, resolu depuis l'ad_id du referral.",
            ),
        ),
    ]
