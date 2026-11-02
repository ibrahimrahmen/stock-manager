from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0105_offer_shop_hidden"),
    ]

    operations = [
        migrations.AddField(
            model_name="customer",
            name="email",
            field=models.EmailField(blank=True, default="", max_length=254),
        ),
    ]
