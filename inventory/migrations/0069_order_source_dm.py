from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0068_conv_campaign_name"),
    ]

    operations = [
        migrations.AlterField(
            model_name="order",
            name="source",
            field=models.CharField(
                max_length=20,
                choices=[
                    ("web_form", "Saisie manuelle"),
                    ("shopify", "Shopify"),
                    ("converty", "Converty"),
                    ("messenger", "Messenger"),
                    ("instagram", "Instagram"),
                ],
                default="web_form",
            ),
        ),
    ]
