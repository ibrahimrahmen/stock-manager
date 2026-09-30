from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0094_producttest_facebook_link'),
    ]

    operations = [
        migrations.AddField(
            model_name='producttest',
            name='production_stage',
            field=models.CharField(
                choices=[
                    ('en_production', 'En Production'),
                    ('patronnage', 'Patronnage'),
                    ('cherche_tissu', 'Cherche Tissu'),
                    ('echantillon', 'Échantillon'),
                    ('fil_chaine', 'Fil Chaîne'),
                    ('broderie', 'Broderie'),
                    ('matelassage', 'Matelassage'),
                    ('serigraphie', 'Sérigraphie'),
                    ('finition', 'Finition'),
                ],
                default='en_production', max_length=30),
        ),
    ]
