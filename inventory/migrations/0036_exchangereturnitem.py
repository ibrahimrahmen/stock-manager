from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0035_order_status_livree"),
    ]

    operations = [
        migrations.CreateModel(
            name="ExchangeReturnItem",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("size", models.CharField(blank=True, default="", max_length=20)),
                ("product_name_snapshot", models.CharField(blank=True, default="", help_text="Name at time of return creation, for display.", max_length=200)),
                ("status", models.CharField(choices=[("pending", "En attente"), ("received", "Reçu"), ("missing", "Manquant")], default="pending", max_length=20)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("exchange_order", models.ForeignKey(help_text="L'Order qui est l'échange (pas la commande originale livrée).", on_delete=django.db.models.deletion.CASCADE, related_name="return_items", to="inventory.order")),
                ("unit", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="exchange_returns", to="inventory.productunit")),
                ("variant", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="exchange_returns", to="inventory.productvariant")),
            ],
            options={
                "indexes": [models.Index(fields=["exchange_order", "status"], name="inventory_e_exchang_a7e8c6_idx")],
            },
        ),
    ]
