from django.db import migrations, models
import django.db.models.deletion


def seed_links_from_legacy(apps, schema_editor):
    """Create an AdOfferLink from each ad's legacy single `offer`. Sales page is
    left null (unknown historically); the user re-picks the page in the UI."""
    Ad = apps.get_model("inventory", "Ad")
    AdOfferLink = apps.get_model("inventory", "AdOfferLink")
    for ad in Ad.objects.all():
        if ad.offer_id:
            AdOfferLink.objects.get_or_create(ad_id=ad.id, offer_id=ad.offer_id,
                                              sales_page=None)


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
        migrations.AlterField(
            model_name="ad",
            name="offer",
            field=models.ForeignKey(
                blank=True,
                help_text="(Ancien) Offre unique liée. Utiliser plutôt les liens.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="ads",
                to="inventory.offer",
            ),
        ),
        migrations.CreateModel(
            name="AdOfferLink",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("ad", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="links", to="inventory.ad")),
                ("offer", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="ad_links", to="inventory.offer")),
                ("sales_page", models.ForeignKey(blank=True, help_text="Page de vente. Vide = toutes pages (rare).", null=True, on_delete=django.db.models.deletion.CASCADE, related_name="ad_links", to="inventory.salespage")),
            ],
            options={"unique_together": {("ad", "offer", "sales_page")}},
        ),
        migrations.AddField(
            model_name="ad",
            name="offers",
            field=models.ManyToManyField(
                blank=True,
                help_text="1 ou 2 paires (offre, page) liees a cette pub.",
                related_name="linked_ads",
                through="inventory.AdOfferLink",
                to="inventory.offer",
            ),
        ),
        migrations.RunPython(seed_links_from_legacy, noop_reverse),
    ]
