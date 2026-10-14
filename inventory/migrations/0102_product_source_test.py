from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0101_producttest_spending_log'),
    ]

    operations = [
        migrations.AddField(
            model_name='product',
            name='source_test',
            field=models.ForeignKey(
                null=True, blank=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='catalog_products', to='inventory.producttest',
                help_text='Test de production d\'où ce produit a été créé.'),
        ),
    ]
