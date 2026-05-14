from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0031_order_status_supprime_navex"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="scheduled_for",
            field=models.DateField(
                blank=True,
                db_index=True,
                help_text="Date à laquelle traiter la commande. NULL = pas de planification (= aujourd'hui).",
                null=True,
            ),
        ),
    ]
