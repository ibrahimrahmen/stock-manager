from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0060_messengerconversation_auto_replied"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="sms_created_sent",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="order",
            name="sms_injoignable_sent",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="order",
            name="sms_expedie_sent",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="order",
            name="sms_en_cours_sent",
            field=models.BooleanField(default=False),
        ),
    ]
