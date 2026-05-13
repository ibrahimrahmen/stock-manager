from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0028_customer_phone2"),
    ]

    operations = [
        migrations.CreateModel(
            name="Delegation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120)),
                ("is_active", models.BooleanField(default=True)),
                ("region", models.ForeignKey(
                    on_delete=models.deletion.CASCADE,
                    related_name="delegations",
                    to="inventory.region",
                )),
            ],
            options={
                "ordering": ["region__name", "name"],
                "unique_together": {("region", "name")},
            },
        ),
    ]
