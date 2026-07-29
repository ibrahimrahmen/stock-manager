from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0062_messengerconversation_gemini_enriched"),
    ]

    operations = [
        migrations.CreateModel(
            name="Expense",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("amount", models.DecimalField(decimal_places=3, max_digits=12)),
                ("category", models.CharField(db_index=True, max_length=40, choices=[
                    ("Tailor", "Tailor (confection/broderie/coupe)"),
                    ("Tissu", "Tissu"),
                    ("Fourniture", "Fourniture (emballage, cordon, \u00e9tiquettes...)"),
                    ("Fournitures Bureau", "Fournitures Bureau"),
                    ("Salaire", "Salaire / Avance / Prime"),
                    ("CNSS", "CNSS"),
                    ("% des commerciaux", "% des commerciaux"),
                    ("Sponsoring", "Sponsoring (pub externe)"),
                    ("Marketing", "Marketing (shooting, concours)"),
                    ("Bureau", "Bureau / Loyer d\u00e9p\u00f4t"),
                    ("Home", "Home / Loyer maison"),
                    ("SONEDE + STEG", "SONEDE + STEG (eau/\u00e9lectricit\u00e9)"),
                    ("Internet", "Internet"),
                    ("Telecom", "Telecom"),
                    ("ENTRETIEN & MAINTENANCE", "Entretien & Maintenance"),
                    ("Transportation", "Transportation (essence, leasing...)"),
                    ("FullFillment", "FullFillment"),
                    ("Comptable", "Comptable (honoraires)"),
                    ("Recette des finances", "Recette des finances (imp\u00f4ts, DM)"),
                    ("Investissement", "Investissement"),
                    ("Restaurant", "Restaurant"),
                    ("Groceries", "Groceries (d\u00e9jeuner \u00e9quipe)"),
                    ("Pressing", "Pressing"),
                    ("Other", "Autre"),
                ])),
                ("comment", models.CharField(blank=True, default="", max_length=300)),
                ("date", models.DateField(db_index=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["-date", "-created_at"]},
        ),
    ]
