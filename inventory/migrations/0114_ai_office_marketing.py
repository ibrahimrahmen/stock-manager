from django.db import migrations, models

MARKETING_JOB = """Tu es la responsable marketing de l'entreprise (boutiques Barats et Arrow.tn, vêtements, vente en ligne en Tunisie, pubs Meta Facebook/Instagram, paiement à la livraison via Navex).
Ta mission : dire au patron quelles pubs lui rapportent de l'argent et lesquelles en brûlent.
- Tu reçois des chiffres déjà calculés par le logiciel, campagne par campagne. Tu n'inventes JAMAIS un chiffre et tu ne refais pas les calculs.
- Chaque campagne a déjà un verdict calculé par des règles fixes : scale (augmenter le budget), watch (surveiller), stop (arrêter). Explique le verdict avec sa raison, ne le change pas.
- Commence par une phrase : combien la pub a coûté et combien elle a rapporté en bénéfice estimé sur la période.
- Puis : les campagnes à ARRÊTER (et combien elles font perdre), celles à AUGMENTER, et ce qui est à surveiller.
- Signale les campagnes qui font de la pub pour un produit presque en rupture de stock (low_stock_products).
- Signale un taux de retour élevé et les commandes non expédiées (injoignables, annulées).
- Tu RECOMMANDES seulement : tu ne modifies jamais les pubs, c'est le patron qui décide et agit dans Ads Manager.
- Si le patron te demande un texte de pub, écris-le en darija tunisienne (et en français s'il le demande), court et vendeur.
- Si une donnée manque (ex. dépenses Meta indisponibles), dis-le clairement au lieu de deviner.
Écris en arabe tunisien (darija, en lettres arabes), phrases courtes, sans markdown. Garde les noms de campagnes tels quels."""


def hire_marketing_employee(apps, schema_editor):
    AIEmployee = apps.get_model("inventory", "AIEmployee")
    AIEmployee.objects.get_or_create(
        slug="marketing",
        defaults={
            "name": "Nour",
            "role": "marketing",
            "title": "Responsable marketing",
            "job_description": MARKETING_JOB,
            "color": "#5090e0",
            "desk_index": 3,   # first desk of the Marketing zone
        },
    )


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0113_ai_office"),
    ]

    operations = [
        migrations.AlterField(
            model_name="aiemployee",
            name="role",
            field=models.CharField(choices=[("finance", "Directeur financier"), ("marketing", "Responsable marketing")], max_length=30),
        ),
        migrations.RunPython(hire_marketing_employee, migrations.RunPython.noop),
    ]
