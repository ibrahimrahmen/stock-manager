from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0097_order_notes_log'),
    ]

    operations = [
        migrations.AddField(
            model_name='producttest',
            name='stage_history',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AlterField(
            model_name='producttest',
            name='production_stage',
            field=models.CharField(
                default='en_production', max_length=30,
                choices=[
                    ('en_production', 'En Production'),
                    ('patronnage', 'Patronnage'),
                    ('cherche_tissu', 'Cherche Tissu'),
                    ('echantillon', 'Échantillon'),
                    ('salle_de_coupe', 'Salle de Coupe'),
                    ('fil_chaine', 'Fil Chaîne'),
                    ('broderie', 'Broderie'),
                    ('matelassage', 'Matelassage'),
                    ('serigraphie', 'Sérigraphie'),
                    ('finition', 'Finition'),
                ]),
        ),
    ]
