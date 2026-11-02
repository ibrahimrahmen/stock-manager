from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0103_create_cache_table"),
    ]

    operations = [
        migrations.AddField(
            model_name="offer",
            name="sort_order",
            field=models.PositiveIntegerField(blank=True, db_index=True, null=True),
        ),
    ]
