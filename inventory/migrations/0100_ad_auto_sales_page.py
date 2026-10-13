from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0099_producttest_priority'),
    ]

    operations = [
        migrations.AddField(
            model_name='ad',
            name='auto_sales_page',
            field=models.ForeignKey(
                null=True, blank=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='auto_ads', to='inventory.salespage',
                help_text="Page de vente détectée automatiquement depuis Meta "
                          "(destination + page de la pub). Utilisée seulement si "
                          "la pub n'a pas de lien manuel."),
        ),
        migrations.AddField(
            model_name='ad',
            name='page_detected_at',
            field=models.DateTimeField(null=True, blank=True),
        ),
    ]
