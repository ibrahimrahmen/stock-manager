from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0070_expense"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="sms_en_cours_last_tel",
            field=models.CharField(blank=True, default="", max_length=30),
        ),
        migrations.AddField(
            model_name="order",
            name="sms_en_cours_last_date",
            field=models.DateField(blank=True, null=True),
        ),
    ]
