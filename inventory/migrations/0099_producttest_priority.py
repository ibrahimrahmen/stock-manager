from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0098_producttest_stage_history'),
    ]

    operations = [
        migrations.AddField(
            model_name='producttest',
            name='priority',
            field=models.CharField(
                default='medium', max_length=10, db_index=True,
                choices=[('high', 'Haute'), ('medium', 'Moyenne'), ('low', 'Basse')]),
        ),
    ]
