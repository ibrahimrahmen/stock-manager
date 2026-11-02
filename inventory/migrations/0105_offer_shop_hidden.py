from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0104_offer_sort_order"),
    ]

    operations = [
        migrations.AddField(
            model_name="offer",
            name="shop_hidden",
            field=models.BooleanField(default=False, db_index=True),
        ),
    ]
