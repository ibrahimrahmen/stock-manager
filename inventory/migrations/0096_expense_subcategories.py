from django.db import migrations, models


OLD_TO_NEW = {
    "Tissu": "tissu",
    "Tailor": "confection",
    "Fourniture": "fourniture_prod",
    "Fournitures Bureau": "fournitures_bureau",
    "Salaire": "salaire",
    "CNSS": "cnss",
    "% des commerciaux": "commerciaux",
    "Sponsoring": "sponsoring",
    "Marketing": "marketing_autre",
    "Bureau": "loyer_pro",
    "Home": "loyer_logement",
    "SONEDE + STEG": "sonede_steg",
    "Internet": "internet",
    "Telecom": "telecom",
    "ENTRETIEN & MAINTENANCE": "maintenance",
    "Transportation": "carburant",
    "FullFillment": "fullfillment",
    "Comptable": "comptable",
    "Recette des finances": "impots",
    "Investissement": "investissement",
    "Restaurant": "repas",
    "Groceries": "repas",
    "Cafe": "repas",
    "Pressing": "pressing",
    "Other": "autre",
}

NEW_KEYS = {
    "tissu", "confection", "fourniture_prod", "sponsoring", "marketing_autre",
    "salaire", "avance", "cnss", "commerciaux", "loyer_pro", "loyer_logement",
    "amenagement", "materiel_info", "fournitures_bureau", "logiciels",
    "investissement", "carburant", "leasing", "entretien_vehicule", "comptable",
    "impots", "bancaire", "internet", "telecom", "sonede_steg", "maintenance",
    "repas", "fullfillment", "pressing", "autre",
}


def migrate_categories(apps, schema_editor):
    Expense = apps.get_model("inventory", "Expense")
    for e in Expense.objects.all():
        c = (e.category or "").strip()
        if c in NEW_KEYS:
            continue
        new = OLD_TO_NEW.get(c, "autre")
        e.category = new
        e.save(update_fields=["category"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0095_producttest_production_stage'),
    ]

    operations = [
        migrations.RunPython(migrate_categories, noop),
    ]
