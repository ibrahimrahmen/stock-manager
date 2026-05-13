from django.db import migrations


# Map from "Navex name" (from their backend) to "DB Region name"
# (already seeded with accents in migration 0017).
# This is CRITICAL — if mismatched, we'd create duplicate Region rows.
NAVEX_TO_DB_REGION = {
    "Beja": "Béja",
    "Tunis": "Tunis",
    "Kasserine": "Kasserine",
    "Sidi Bouzid": "Sidi Bouzid",
    "Le Kef": "LE Kef",
    "Ariana": "Ariana",
    "Tozeur": "Tozeur",
    "Tataouine": "Tataouine",
    "Mahdia": "Mahdia",
    "Zaghouan": "Zaghouan",
    "Nabeul": "Nabeul",
    "Gafsa": "Gafsa",
    "Sousse": "Sousse",
    "Monastir": "Monastir",
    "Siliana": "Siliana",
    "La Manouba": "La Manouba",
    "Sfax": "Sfax",
    "Kebili": "Kébili",
    "Medenine": "Médenine",
    "Gabes": "Gabès",
    "Jendouba": "Jendouba",
    "Bizerte": "Bizerte",
    "Kairouan": "Kairouan",
    "Ben Arous": "Ben Arous",
}


# Full list of Tunisian delegations grouped by governorate.
# Source: Navex backend capture (May 2026).
DELEGATIONS = {
    "Beja": ["Amdoun", "Beja Nord", "Beja", "Dougga", "El Maagoula", "Goubellat",
             "Mejez El Bab", "Nefza", "Ouchtata-Jmila", "Sidi Ismail", "Slouguia",
             "Teboursouk", "Testour", "Thibar"],
    "Tunis": ["Ain Zaghouan", "Bab Bhar", "Bab Saadoun", "Bab Souika", "Carthage",
              "Cite El Khadra", "El Hrairia", "El Kabbaria", "El Kram", "El Menzah",
              "El Omrane", "El Omrane Superieur", "El Ouerdia", "Essijoumi",
              "Ettahrir", "Ezzouhour", "Jebel Jelloud", "La Goulette", "La Marsa",
              "La Medina", "Le Bardo", "Sidi El Bechir", "Sidi Hassine"],
    "Kasserine": ["El Ayoun", "Ezzouhour", "Feriana", "Foussana", "Haidra",
                  "Hassi El Frid", "Kasserine Nord", "Kasserine Sud",
                  "Mejel Bel Abbes", "Sbeitla", "Sbiba", "Thala"],
    "Sidi Bouzid": ["Ben Oun", "Bir El Haffey", "Cebbala", "Jilma", "Maknassy",
                    "Menzel Bouzaiene", "Mezzouna", "Ouled Haffouz", "Regueb",
                    "Sidi Bouzid Est", "Sidi Bouzid Ouest", "Souk Jedid"],
    "Le Kef": ["Dahmani", "El Ksour", "Jerissa", "Kalaa El Khasba", "Kalaat Sinane",
               "Le Kef Est", "Le Kef Ouest", "Le Sers", "Nebeur",
               "Sakiet Sidi Youssef", "Tajerouine", "Touiref"],
    "Ariana": ["Ariana Ville", "Ennast", "Ettadhamen", "Kalaat Landlous",
               "La Soukra", "Mnihla", "Raoued", "Sidi Thabet"],
    "Tozeur": ["Degueche", "Hezoua", "Nefta", "Tameghza"],
    "Tataouine": ["Bir Lahmar", "Dhehiba", "Ghomrassen", "Remada", "Smar",
                  "Tataouine Nord", "Tataouine Sud"],
    "Mahdia": ["Bou Merdes", "Chiba", "El Hekaima", "El Jem", "Ezzahra", "Hbira",
               "Hiboun", "Jouaouda", "Ksour Essaf", "La Chebba", "Melloulech",
               "Ouled Chamakh", "Rejiche", "Sidi Alouene", "Souassi", "Zouaouine"],
    "Zaghouan": ["Bir Mcherga", "El Fahs", "Ennadhour", "Hammam Zriba", "Saouef"],
    "Nabeul": ["Barraket Essahel", "Beni Khalled", "Beni Khiar", "Bou Argoub",
               "Dar Chaabane Elfehri", "El Haouaria", "El Mida", "Grombalia",
               "Hammamet", "Kelibia", "Korba", "Menzel Bouzelfa", "Menzel Temime",
               "Soliman", "Takelsa"],
    "Gafsa": ["Belkhir", "El Guettar", "El Ksar", "El Mdhilla", "Gafsa Nord",
              "Gafsa Sud", "Metlaoui", "Moulares", "Redeyef", "Sidi Aich", "Sned"],
    "Sousse": ["Akouda", "Bou Ficha", "Bouhcina", "Enfidha", "Hammam Sousse",
               "Hergla", "Kalaa El Kebira", "Kalaa Essghira", "Khezama", "Kondar",
               "Msaken", "Sidi Bou Ali", "Sidi El Heni", "Sousse Jaouhara",
               "Sousse Riadh", "Sousse Ville", "Zaouia / kssiba / Thrayette"],
    "Monastir": ["Bekalta", "Bembla", "Beni Hassen", "Jemmal", "Khenis",
                 "Ksar Helal", "Ksibet El Mediouni", "Manzel Kamel", "Manzel nour",
                 "Moknine", "Ouerdanine", "Sahline", "Sayada Lamta Bou Hajar",
                 "Teboulba", "Zeramdine"],
    "Siliana": ["Bargou", "Bou Arada", "El Aroussa", "Gaafour", "Le Krib",
                "Makthar", "Rohia", "Sidi Bou Rouis", "Siliana Nord", "Siliana Sud"],
    "La Manouba": ["Borj El Amri", "Denden", "Douar Hicher", "Jedaida", "Mannouba",
                   "Mornaguia", "Oued Ellil", "Tebourba"],
    "Sfax": ["Agareb", "Bir Ali Ben Khelifa", "El Amra", "El Hencha", "Esskhira",
             "Ghraiba", "Jebeniana", "Kerkenah", "Mahras", "Menzel Chaker",
             "Sakiet Eddaier", "Sakiet Ezzit", "Sfax Est", "Sfax Sud", "Sfax Ville"],
    "Kebili": ["Douz", "El Faouar", "Kebili Nord", "Kebili Sud", "Souk El Ahad"],
    "Medenine": ["Ajim", "Ben Guerdane", "Beni Khedache", "Djerba (Houmet Essouk)",
                 "Djerba (Midoun)", "Medenine Nord", "Medenine Sud", "Sidi Makhlouf", "Zarzis"],
    "Gabes": ["El Hamma", "El Metouia", "Gabes Medina", "Gabes Ouest", "Gabes Sud",
              "Ghannouche", "Mareth", "Matmata", "Menzel Habib", "Nouvelle Matmata",
              "Oudhref"],
    "Jendouba": ["Ain Draham", "Balta Bou Aouene", "Bou Salem", "Fernana",
                 "Ghardimaou", "Jendouba Nord", "Oued Mliz", "Tabarka"],
    "Bizerte": ["Bizerte Nord", "Bizerte Sud", "El Alia", "El Hachachna",
                "Ghar El Melh", "Ghezala", "Jarzouna", "Joumine", "Mateur",
                "Menzel Bourguiba", "Menzel Jemil", "Ras Jebel", "Sejnane",
                "Tinja", "Utique", "Menzel Abderrahmane"],
    "Kairouan": ["Bou Hajla", "Chebika", "Cherarda", "Haffouz", "Hajeb El Ayoun",
                 "Kairouan Nord", "Kairouan Sud", "Nasrallah", "Oueslatia", "Sbikha"],
    "Ben Arous": ["Bou Mhel El Bassatine", "El Mourouj", "Ezzahra", "Fouchana",
                  "Hammam Chatt", "Hammam Lif", "Khalidia", "Megrine", "Mohamadia",
                  "Mornag", "Naassen", "Nouvelle Medina", "Rades"],
}


def seed_delegations(apps, schema_editor):
    Region = apps.get_model("inventory", "Region")
    Delegation = apps.get_model("inventory", "Delegation")
    for navex_name, delegation_names in DELEGATIONS.items():
        db_region_name = NAVEX_TO_DB_REGION.get(navex_name, navex_name)
        # Only seed for regions that already exist — never create new ones here
        try:
            region = Region.objects.get(name=db_region_name)
        except Region.DoesNotExist:
            # Skip silently — region wasn't seeded yet for some reason
            continue
        for d_name in delegation_names:
            Delegation.objects.get_or_create(
                region=region, name=d_name, defaults={"is_active": True}
            )


def unseed_delegations(apps, schema_editor):
    # Reversible: delete all seeded delegations
    Delegation = apps.get_model("inventory", "Delegation")
    Delegation.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0029_delegation"),
    ]

    operations = [
        migrations.RunPython(seed_delegations, reverse_code=unseed_delegations),
    ]
