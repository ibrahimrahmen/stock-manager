from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0027_userprofile_theme"),
    ]

    operations = [
        migrations.AddField(
            model_name="customer",
            name="phone2",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Numéro secondaire optionnel (ex: domicile, conjoint). Envoyé à Navex comme tel2.",
                max_length=20,
            ),
        ),
    ]
