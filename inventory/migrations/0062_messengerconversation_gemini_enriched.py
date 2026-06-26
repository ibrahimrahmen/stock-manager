from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0061_order_sms_flags"),
    ]

    operations = [
        migrations.AddField(
            model_name="messengerconversation",
            name="gemini_enriched",
            field=models.BooleanField(default=False),
        ),
    ]
