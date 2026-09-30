from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0090_producttest_results'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='sent_to_test',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='test_orders', to='inventory.producttest'),
        ),
    ]
