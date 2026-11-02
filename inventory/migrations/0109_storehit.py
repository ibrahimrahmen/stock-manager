from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0108_set_shop_pixel"),
    ]

    operations = [
        migrations.CreateModel(
            name="StoreHit",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True,
                                        serialize=False, verbose_name="ID")),
                ("session", models.CharField(db_index=True, max_length=40)),
                ("event", models.CharField(db_index=True, max_length=20)),
                ("path", models.CharField(blank=True, default="", max_length=220)),
                ("value", models.DecimalField(blank=True, decimal_places=3,
                                              max_digits=10, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
            ],
        ),
        migrations.AddIndex(
            model_name="storehit",
            index=models.Index(fields=["created_at"], name="inv_hit_created_idx"),
        ),
        migrations.AddIndex(
            model_name="storehit",
            index=models.Index(fields=["event", "created_at"], name="inv_hit_evt_idx"),
        ),
        migrations.AddIndex(
            model_name="storehit",
            index=models.Index(fields=["session", "created_at"], name="inv_hit_sess_idx"),
        ),
    ]
