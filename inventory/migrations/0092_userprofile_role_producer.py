from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0091_order_sent_to_test'),
    ]

    operations = [
        migrations.AlterField(
            model_name='userprofile',
            name='role',
            field=models.CharField(
                choices=[
                    ('shipping', 'Shipping'),
                    ('office', 'Office'),
                    ('messages', 'Messages Team'),
                    ('producer', 'Producteur (Testing & Production)'),
                ],
                default='office', max_length=20),
        ),
    ]
