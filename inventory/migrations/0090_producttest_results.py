from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0089_userprofile_must_change_password'),
    ]

    operations = [
        migrations.AddField(
            model_name='producttest',
            name='results',
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='producttest',
            name='total_orders',
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name='producttest',
            name='amount_spent',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.AddField(
            model_name='producttest',
            name='finished_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
