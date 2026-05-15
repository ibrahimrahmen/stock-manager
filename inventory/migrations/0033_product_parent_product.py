from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0032_order_scheduled_for"),
    ]

    operations = [
        migrations.AddField(
            model_name="product",
            name="parent_product",
            field=models.ForeignKey(
                blank=True,
                help_text="Si ce produit est une V2/V3 d'un autre produit (même SKU physique), choisir le produit parent ici.",
                null=True,
                on_delete=models.deletion.SET_NULL,
                related_name="versions",
                to="inventory.product",
            ),
        ),
    ]
