from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0092_userprofile_role_producer'),
    ]

    operations = [
        migrations.AddField(
            model_name='producttest',
            name='production_started',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='producttest',
            name='production_plan',
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='producttest',
            name='producer',
            field=models.ForeignKey(blank=True, help_text='Employé (rôle Producteur) chargé de la production.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='produced_tests', to='auth.user'),
        ),
        migrations.AddField(
            model_name='producttest',
            name='production_started_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
