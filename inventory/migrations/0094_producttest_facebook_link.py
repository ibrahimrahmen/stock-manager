from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0093_producttest_production'),
    ]

    operations = [
        migrations.AddField(
            model_name='producttest',
            name='facebook_link',
            field=models.CharField(blank=True, default='', help_text='Lien vers la publication Facebook/Instagram du produit.', max_length=500),
        ),
    ]
