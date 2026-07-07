from django.db import migrations, models


def copy_offer_to_m2m(apps, schema_editor):
    """Seed the new M2M `offers` from the legacy single `offer` FK, and set a
    sensible attribution: if the ad's linked offer sells only on Barats.tn,
    mark it as the Barats carousel pool; otherwise 'offer'."""
    Ad = apps.get_model("inventory", "Ad")
    for ad in Ad.objects.all():
        if ad.offer_id:
            ad.offers.add(ad.offer_id)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0062_messengerconversation_gemini_enriched"),
    ]

    operations = [
        migrations.AddField(
            model_name="ad",
            name="attribution",
            field=models.CharField(
                choices=[("offer", "Offre(s) liée(s)"), ("barats", "Carrousel Barats.tn")],
                default="offer",
                help_text="Comment cette pub est attribuée : à des offres précises, ou au pool Barats.tn.",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="ad",
            name="offers",
            field=models.ManyToManyField(
                blank=True,
                help_text="1 ou 2 offres liées à cette pub (Converty/Facebook).",
                related_name="linked_ads",
                to="inventory.offer",
            ),
        ),
        migrations.AlterField(
            model_name="ad",
            name="offer",
            field=models.ForeignKey(
                blank=True,
                help_text="(Ancien) Offre unique liée. Utiliser plutôt 'offers'.",
                null=True,
                on_delete=models.SET_NULL,
                related_name="ads",
                to="inventory.offer",
            ),
        ),
        migrations.RunPython(copy_offer_to_m2m, noop_reverse),
    ]
