from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0087_producttestvariant'),
    ]

    operations = [
        migrations.AddField(
            model_name='producttest',
            name='sales_page',
            field=models.ForeignKey(blank=True, help_text='Page où le produit est testé.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='product_tests', to='inventory.salespage'),
        ),
    ]
