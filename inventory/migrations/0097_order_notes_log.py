from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0096_expense_subcategories'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='notes_log',
            field=models.JSONField(
                blank=True, default=list,
                help_text="Historique des notes : chaque note garde sa propre date."),
        ),
    ]
