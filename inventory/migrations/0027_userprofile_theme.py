from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0026_product_season"),
    ]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="theme",
            field=models.CharField(
                choices=[("dark", "Sombre"), ("light", "Clair")],
                default="dark",
                max_length=10,
            ),
        ),
    ]
