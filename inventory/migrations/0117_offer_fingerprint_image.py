"""Dedicated per-offer fingerprint photo bank (OfferFingerprintImage).

Separate from OfferImage (which is shown to customers). These photos exist only
to recognise a product from a customer's picture via perceptual hashing, so the
operator can dump every real circulating image (ad creatives, screenshots, all
colours/angles) without polluting the customer-facing gallery.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0116_deactivate_ai_office"),
    ]

    operations = [
        migrations.CreateModel(
            name="OfferFingerprintImage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("image", models.ImageField(upload_to="offer_fp/")),
                ("note", models.CharField(blank=True, default="", max_length=80,
                                          help_text="Optionnel : d'où vient la photo (ex: pub, story).")),
                ("sort", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("offer", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="fingerprint_images",
                    to="inventory.offer")),
            ],
            options={
                "ordering": ["sort", "id"],
            },
        ),
    ]
