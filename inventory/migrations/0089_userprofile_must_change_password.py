from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0088_producttest_sales_page'),
    ]

    operations = [
        migrations.AddField(
            model_name='userprofile',
            name='must_change_password',
            field=models.BooleanField(default=False, help_text="Si vrai, l'utilisateur doit définir un nouveau mot de passe à sa prochaine connexion (mot de passe temporaire)."),
        ),
    ]
