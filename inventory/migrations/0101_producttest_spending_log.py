from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0100_ad_auto_sales_page'),
    ]

    operations = [
        migrations.AddField(
            model_name='producttest',
            name='spending_log',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
