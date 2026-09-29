from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0086_producttest'),
    ]

    operations = [
        migrations.CreateModel(
            name='ProductTestVariant',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('color_label', models.CharField(help_text='Couleur, ex: Bleu', max_length=50)),
                ('sizes', models.CharField(blank=True, default='', help_text='Tailles disponibles, séparées par des virgules, ex: S,M,L,XL', max_length=120)),
                ('image', models.ImageField(blank=True, null=True, upload_to='test_variants/')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('product_test', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='variants', to='inventory.producttest')),
            ],
            options={
                'ordering': ['id'],
            },
        ),
    ]
