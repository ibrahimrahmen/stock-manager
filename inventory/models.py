from django.db import models
from django.utils import timezone


class Product(models.Model):
    name = models.CharField(max_length=255)
    code = models.CharField(max_length=10, unique=True, help_text="Short code used in barcode prefix, e.g. RLF")
    category = models.CharField(max_length=100, blank=True)
    description = models.TextField(blank=True)
    buy_price = models.DecimalField(max_digits=10, decimal_places=3, default=0)
    sell_price = models.DecimalField(max_digits=10, decimal_places=3, default=0)
    alert_threshold = models.PositiveIntegerField(default=5)
    alert_disabled = models.BooleanField(default=False, help_text="Skip low-stock alerts for this product (still active otherwise)")
    archived = models.BooleanField(default=False, help_text="Retired product — hidden from lists and emails, but scanning still works")

    # Season classification — used by the products page to split summer/winter inventory
    SEASON_SUMMER = "summer"
    SEASON_WINTER = "winter"
    SEASON_CHOICES = [
        (SEASON_SUMMER, "Été"),
        (SEASON_WINTER, "Hiver"),
    ]
    season = models.CharField(
        max_length=10, choices=SEASON_CHOICES, default=SEASON_SUMMER,
        help_text="Saison du produit — utilisée pour filtrer la liste des produits",
    )
    # Optional link to a "parent" product: used when V2/V3 versions of a product
    # share the same physical SKU. Stock is summed across parent + children when
    # checking availability for shipping. Leave NULL for independent products.
    parent_product = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="versions",
        help_text="Si ce produit est une V2/V3 d'un autre produit (même SKU physique), choisir le produit parent ici."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({self.code})"

    @property
    def total_stock(self):
        return ProductUnit.objects.filter(
            variant__product=self,
            status__in=(ProductUnit.IN_STOCK, ProductUnit.RETURNED)
        ).count()

    @property
    def family_root(self):
        """The root product of this SKU family (self if it has no parent)."""
        return self.parent_product or self

    def family_products(self):
        """All products sharing this physical SKU (root + its versions)."""
        from django.db.models import Q
        root = self.family_root
        return Product.objects.filter(Q(id=root.id) | Q(parent_product=root))

    @property
    def family_total_stock(self):
        """Sellable stock summed across the whole SKU family (parent + V2/V3)."""
        fam_ids = list(self.family_products().values_list("id", flat=True))
        return ProductUnit.objects.filter(
            variant__product_id__in=fam_ids,
            status__in=(ProductUnit.IN_STOCK, ProductUnit.RETURNED)
        ).count()


class ProductVariant(models.Model):
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="variants")
    color_name = models.CharField(max_length=50, help_text="e.g. RED, GRN, BLU")
    color_label = models.CharField(max_length=50, help_text="Display name e.g. Rouge")
    image = models.ImageField(upload_to="variants/", blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("product", "color_name")

    def __str__(self):
        return f"{self.product.name} — {self.color_label}"

    def save(self, *args, **kwargs):
        # On save, if the image was just (re)uploaded, resize it down so we
        # don't store 8MB WhatsApp photos. Max 1200×1200, JPEG quality 80.
        # Only runs when image is new (has 'file' attribute) — avoids re-resizing
        # when only other fields change.
        super().save(*args, **kwargs)
        if self.image:
            try:
                _resize_image_in_place(self.image.path, max_size=1200, quality=80)
            except Exception:
                # Don't crash the save if resize fails — log silently
                pass

    @property
    def stock_by_size(self):
        sizes = {}
        for unit in self.units.filter(status__in=(ProductUnit.IN_STOCK, ProductUnit.RETURNED)):
            sizes[unit.size] = sizes.get(unit.size, 0) + 1
        return sizes

    @property
    def total_stock(self):
        return self.units.filter(status__in=(ProductUnit.IN_STOCK, ProductUnit.RETURNED)).count()


def _resize_image_in_place(path, max_size=1200, quality=80):
    """Resize an image file on disk, in place, to a max bounding box.
    Skips images that are already small enough.
    Preserves PNG transparency where applicable; converts huge JPEGs.
    """
    import os
    from PIL import Image
    if not os.path.isfile(path):
        return
    with Image.open(path) as img:
        # Skip if already smaller than threshold (don't waste CPU)
        if max(img.width, img.height) <= max_size:
            # Re-save only if file is huge (>500KB) to recompress
            if os.path.getsize(path) <= 500_000:
                return
        # Preserve format. JPEG-friendly conversion for non-RGB images.
        original_format = (img.format or "JPEG").upper()
        if original_format == "PNG" and img.mode in ("RGBA", "LA"):
            # Keep transparency for PNG
            img.thumbnail((max_size, max_size), Image.LANCZOS)
            img.save(path, format="PNG", optimize=True)
        else:
            # Convert RGBA/P to RGB for JPEG
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.thumbnail((max_size, max_size), Image.LANCZOS)
            img.save(path, format="JPEG", quality=quality, optimize=True, progressive=True)


class ProductUnit(models.Model):
    IN_STOCK      = "in_stock"
    SHIPPED       = "shipped"
    PAID          = "paid"
    RETURNED      = "returned"
    EARLY_RETURN  = "early_return"  # Navex returned status "Rtn client/agence" — customer refused, en route back
    AT_DEPOT      = "at_depot"      # Navex "Retour Depot Navex" — arrived at Navex hub, waiting for our physical pickup

    STATUS_CHOICES = [
        (IN_STOCK,     "En stock"),
        (SHIPPED,      "Expédié"),
        (PAID,         "Payé"),
        (RETURNED,     "Retourné"),
        (EARLY_RETURN, "Retour anticipé"),
        (AT_DEPOT,     "Retour en dépôt Navex"),
    ]

    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT, related_name="units")
    barcode = models.CharField(max_length=100, unique=True)
    size = models.CharField(max_length=20, help_text="Ex: S, M, XL, 40, 41, 1, 2, UNIQUE...")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=IN_STOCK)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.variant} / {self.size} — {self.barcode}"


class ShippingOrder(models.Model):
    OPEN             = "open"
    CLOSED           = "closed"
    PAID             = "paid"
    PARTIAL_PAID     = "partial_paid"
    RETURNED         = "returned"
    PARTIAL_RETURNED = "partial_returned"
    LIVRE            = "livre"
    PARTIAL_LIVRE    = "partial_livre"

    STATUS_CHOICES = [
        (OPEN,             "Ouvert"),
        (CLOSED,           "Fermé"),
        (PAID,             "Payé"),
        (PARTIAL_PAID,     "Partiellement payé"),
        (RETURNED,         "Retourné"),
        (PARTIAL_RETURNED, "Partiellement retourné"),
        (LIVRE,            "Livré"),
        (PARTIAL_LIVRE,    "Partiellement livré"),
    ]

    # Groups for business logic
    PAID_STATUSES    = (PAID, PARTIAL_PAID)
    LIVRE_STATUSES   = (LIVRE, PARTIAL_LIVRE)
    RETURNED_STATUSES = (RETURNED, PARTIAL_RETURNED)
    CLOSED_STATUSES  = (CLOSED, PAID, PARTIAL_PAID, RETURNED, PARTIAL_RETURNED, LIVRE, PARTIAL_LIVRE)

    SHIPPING_FEE = 7  # TND fixed shipping fee charged to client

    bordereau_barcode    = models.CharField(max_length=255, unique=True)
    payment_barcode      = models.CharField(max_length=255, blank=True, null=True, unique=True,
                                            help_text="Barcode from shipping company payment slip")
    shipping_company     = models.CharField(max_length=100, blank=True)
    status               = models.CharField(max_length=20, choices=STATUS_CHOICES, default=OPEN)
    opened_at            = models.DateTimeField(auto_now_add=True)
    closed_at            = models.DateTimeField(null=True, blank=True)
    paid_at              = models.DateTimeField(null=True, blank=True)
    # When the Navex sync first saw a "Livré Payé" status for this order. Used
    # as the paid_at date when the office later confirms the payment, so paid_at
    # reflects when Navex reported it paid (≈ real date) rather than the click.
    navex_paid_detected_at = models.DateTimeField(null=True, blank=True)
    amount_collected     = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True,
                                               help_text="Actual amount collected from client")
    notes                = models.TextField(blank=True)
    # Client info from Navex
    client_name       = models.CharField(max_length=200, blank=True, default="")
    client_phone      = models.CharField(max_length=50, blank=True, default="")
    client_address    = models.CharField(max_length=500, blank=True, default="")
    client_ville      = models.CharField(max_length=100, blank=True, default="")
    navex_designation = models.CharField(max_length=500, blank=True, default="")

    # Link to the v2 Order this shipping was created for, if applicable.
    # Set automatically at scan-expedition time when the bordereau matches an
    # Order in the v2 system. Allows cross-navigation between v1 and v2.
    order = models.ForeignKey(
        "Order", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="shipping_orders",
        help_text="Order v2 lié à ce ShippingOrder, si applicable.",
    )

    def __str__(self):
        return f"Ordre {self.bordereau_barcode} ({self.status})"

    @property
    def unit_count(self):
        return self.items.count()

    @property
    def paid_items(self):
        return self.items.filter(unit__status=ProductUnit.PAID)

    @property
    def returned_items(self):
        return self.items.filter(unit__status=ProductUnit.RETURNED)

    @property
    def expected_amount(self):
        total = sum(item.unit.variant.product.sell_price for item in self.paid_items)
        return total + self.SHIPPING_FEE

    @property
    def is_overdue(self):
        """True if closed but not paid for more than 5 days."""
        if self.status == self.PAID:
            return False
        if self.status == self.CLOSED and self.closed_at:
            delta = timezone.now() - self.closed_at
            return delta.days >= 5
        return False


class OrderItem(models.Model):
    order           = models.ForeignKey(ShippingOrder, on_delete=models.PROTECT, related_name="items")
    unit            = models.ForeignKey(ProductUnit, on_delete=models.PROTECT, related_name="order_items")
    scanned_at      = models.DateTimeField(auto_now_add=True)
    # Snapshot of unit status at the time of each key event — never changes retroactively
    status_at_scan  = models.CharField(max_length=20, blank=True, default="in_order")
    status_at_close = models.CharField(max_length=20, blank=True, null=True)
    status_at_payment = models.CharField(max_length=20, blank=True, null=True)

    class Meta:
        unique_together = ("order", "unit")

    def __str__(self):
        return f"{self.order} — {self.unit.barcode}"

    @property
    def display_status(self):
        """
        Return the status of this unit IN THE CONTEXT OF THIS ORDER.
        Always uses snapshots — never the current live unit status.
        This prevents a unit re-used in another order from corrupting this order's display.
        """
        if self.status_at_payment:
            return self.status_at_payment
        if self.status_at_close:
            return self.status_at_close
        if self.status_at_scan:
            return self.status_at_scan
        # Fallback only if no snapshot exists (legacy data)
        return self.unit.status

    @property
    def display_status_label(self):
        labels = {
            "in_stock": "En stock",
            "shipped": "Expédié",
            "paid": "Payé",
            "returned": "Retourné",
            "refused_waiting": "Refusé — en attente",
            "shipped_refused": "Refusé — en attente",
        }
        return labels.get(self.display_status, self.display_status)


class Payment(models.Model):
    order            = models.OneToOneField(ShippingOrder, on_delete=models.PROTECT, related_name="payment")
    payment_barcode  = models.CharField(max_length=255, blank=True)
    amount_expected  = models.DecimalField(max_digits=10, decimal_places=3)
    amount_collected = models.DecimalField(max_digits=10, decimal_places=3)
    shipping_fee     = models.DecimalField(max_digits=10, decimal_places=3, default=7)
    notes            = models.TextField(blank=True)
    created_at       = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Paiement ordre {self.order.bordereau_barcode} — {self.amount_collected} TND"


class NavexSyncLog(models.Model):
    """Tracks Navex sync results for shipped orders."""
    order = models.ForeignKey(ShippingOrder, on_delete=models.CASCADE, related_name="navex_logs")
    navex_status = models.CharField(max_length=100, blank=True)
    navex_amount = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    our_amount = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    amount_match = models.BooleanField(null=True)
    raw_response = models.TextField(blank=True)
    synced_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-synced_at"]

    def __str__(self):
        return f"{self.order.bordereau_barcode} — Navex: {self.navex_status} @ {self.synced_at:%Y-%m-%d %H:%M}"


class StockMovement(models.Model):
    RECEIVED     = "received"
    SHIPPED      = "shipped"
    PAID         = "paid"
    RETURNED     = "returned"
    EARLY_RETURN = "early_return"
    AT_DEPOT     = "at_depot"

    TYPE_CHOICES = [
        (RECEIVED,     "Réception"),
        (SHIPPED,      "Expédition"),
        (PAID,         "Payé"),
        (RETURNED,     "Retour"),
        (EARLY_RETURN, "Retour anticipé"),
        (AT_DEPOT,     "Retour en dépôt Navex"),
    ]

    unit          = models.ForeignKey(ProductUnit, on_delete=models.PROTECT, related_name="movements")
    movement_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    reference     = models.CharField(max_length=255, blank=True)
    moved_at      = models.DateTimeField(auto_now_add=True)
    # Phase 8: who performed the scan/action. Nullable = old movements before this field.
    user          = models.ForeignKey(
        "auth.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="stock_movements",
    )

    class Meta:
        ordering = ["-moved_at"]

    def __str__(self):
        return f"{self.unit.barcode} — {self.movement_type} @ {self.moved_at:%Y-%m-%d %H:%M}"


class SizeAlert(models.Model):
    """Alert threshold per variant + size combination."""
    variant   = models.ForeignKey(ProductVariant, on_delete=models.CASCADE, related_name="size_alerts")
    size      = models.CharField(max_length=20)
    threshold = models.PositiveIntegerField(default=3, help_text="Alert when stock drops below this number")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("variant", "size")

    def __str__(self):
        return f"{self.variant} / {self.size} — seuil {self.threshold}"

    @property
    def current_stock(self):
        return ProductUnit.objects.filter(
            variant=self.variant,
            size=self.size,
            status__in=(ProductUnit.IN_STOCK, ProductUnit.RETURNED)
        ).count()

    @property
    def is_triggered(self):
        # Predictive: triggered when days-of-cover < ALERT_DAYS at recent rate
        return compute_size_forecast(self.variant, self.size)["is_triggered"]


class OrderVerification(models.Model):
    """Tracks orders that need verification — shipped too long without delivery."""
    order = models.OneToOneField(ShippingOrder, on_delete=models.CASCADE, related_name="verification")
    created_at = models.DateTimeField(auto_now_add=True)
    treated = models.BooleanField(default=False)
    treated_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Verification {self.order.bordereau_barcode} — {'Traité' if self.treated else 'En attente'}"


class ScanSessionLog(models.Model):
    """Daily scan session log — resets each morning."""
    bordereau_barcode = models.CharField(max_length=50)
    designation = models.CharField(max_length=500, blank=True, default="")
    unit_count = models.IntegerField(default=0)
    is_correct = models.BooleanField(default=True)
    reason = models.CharField(max_length=200, blank=True, default="")
    scanned_at = models.DateTimeField(auto_now_add=True)
    session_date = models.DateField()  # date of the session

    class Meta:
        ordering = ["-scanned_at"]

    def __str__(self):
        return f"{self.bordereau_barcode} — {'OK' if self.is_correct else 'WRONG'} @ {self.scanned_at:%Y-%m-%d %H:%M}"


# ---------------------------------------------------------------------------
# PREDICTIVE STOCK FORECASTING
# ---------------------------------------------------------------------------
# Replaces manual SizeAlert thresholds. A (variant, size) is "low" when the
# remaining stock would run out in fewer than ALERT_DAYS at the recent
# net-consumption rate.
FORECAST_WINDOW_DAYS = 7   # average over last 7 days of activity
ALERT_DAYS           = 10  # raise alert when days_of_cover < 10


def compute_family_size_forecast(family_product_ids, size):
    """Like compute_size_forecast but summed across a whole SKU family for a
    given size. Combines stock + movements of that size across all family
    products (parent + V2/V3). Returns the same dict shape."""
    cutoff = timezone.now() - timezone.timedelta(days=FORECAST_WINDOW_DAYS)
    movements = StockMovement.objects.filter(
        unit__variant__product_id__in=family_product_ids,
        unit__size=size,
        moved_at__gte=cutoff,
    ).values_list("movement_type", flat=True)

    shipped  = sum(1 for m in movements if m == StockMovement.SHIPPED)
    returned = sum(1 for m in movements if m == StockMovement.RETURNED)
    net = max(0, shipped - returned)
    daily_rate = net / float(FORECAST_WINDOW_DAYS)

    current_stock = ProductUnit.objects.filter(
        variant__product_id__in=family_product_ids,
        size=size,
        status__in=(ProductUnit.IN_STOCK, ProductUnit.RETURNED),
    ).count()

    if daily_rate <= 0:
        days_of_cover = None
        is_triggered = current_stock == 0
    else:
        days_of_cover = current_stock / daily_rate
        is_triggered = days_of_cover < ALERT_DAYS

    return {
        "current_stock": current_stock,
        "daily_rate": round(daily_rate, 2),
        "days_of_cover": round(days_of_cover, 1) if days_of_cover is not None else None,
        "is_triggered": is_triggered,
    }


def compute_size_forecast(variant, size):
    """Compute days-of-cover for a (variant, size) pair.

    Net daily rate = (shipped events − returned events) over the last
    FORECAST_WINDOW_DAYS, divided by the window length. We use StockMovement
    rows (single source of truth — every shipment and return writes one).

    Returns dict with:
        current_stock, shipped, returned, daily_rate,
        days_of_cover (None if rate is 0), is_triggered
    """
    cutoff = timezone.now() - timezone.timedelta(days=FORECAST_WINDOW_DAYS)
    movements = StockMovement.objects.filter(
        unit__variant=variant,
        unit__size=size,
        moved_at__gte=cutoff,
    ).values_list("movement_type", flat=True)

    shipped  = sum(1 for m in movements if m == StockMovement.SHIPPED)
    returned = sum(1 for m in movements if m == StockMovement.RETURNED)
    net = max(0, shipped - returned)
    daily_rate = net / float(FORECAST_WINDOW_DAYS)

    current_stock = ProductUnit.objects.filter(
        variant=variant,
        size=size,
        status__in=(ProductUnit.IN_STOCK, ProductUnit.RETURNED),
    ).count()

    if daily_rate <= 0:
        days_of_cover = None
        is_triggered = current_stock == 0  # no demand → only alert on zero
    else:
        days_of_cover = current_stock / daily_rate
        is_triggered = days_of_cover < ALERT_DAYS

    return {
        "current_stock": current_stock,
        "shipped": shipped,
        "returned": returned,
        "daily_rate": round(daily_rate, 2),
        "days_of_cover": round(days_of_cover, 1) if days_of_cover is not None else None,
        "is_triggered": is_triggered,
    }


# ---------------------------------------------------------------------------
# V2 ORDER CREATION — Phase 1: SalesPage + Customer
# Additive only. No existing tables touched.
# ---------------------------------------------------------------------------
class SalesPage(models.Model):
    """A sales channel — e.g. 'Barats.tn'. Offers belong to one or more pages."""
    name = models.CharField(max_length=120, unique=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Customer(models.Model):
    """Customer identified by phone number. Same phone = same customer."""
    phone = models.CharField(max_length=20, unique=True)
    phone2 = models.CharField(max_length=20, blank=True, default="",
        help_text="Numéro secondaire optionnel (ex: domicile, conjoint). Envoyé à Navex comme tel2.")
    name = models.CharField(max_length=120, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    # Messenger Page-Scoped ID. Permanent linking key for DM conversations.
    # Kept forever (it is tiny); NOT subject to the 10-day conversation purge.
    # Lets n8n/webhook recognise a returning customer and re-attach a fresh
    # conversation to a new order.
    customer_psid = models.CharField(max_length=64, blank=True, default="", db_index=True,
        help_text="ID Messenger (PSID) du client. Sert à relier les conversations DM.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.phone})" if self.name else self.phone


class Region(models.Model):
    """A Tunisian governorate. Seeded by a data migration with the standard 24."""
    name = models.CharField(max_length=80, unique=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Delegation(models.Model):
    """A sub-administrative unit (délégation) within a Region (governorate).
    Seeded once via data migration to avoid typos when filling out orders.
    """
    region = models.ForeignKey(Region, on_delete=models.CASCADE, related_name="delegations")
    name = models.CharField(max_length=120)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["region__name", "name"]
        unique_together = ("region", "name")

    def __str__(self):
        return f"{self.region.name} — {self.name}"


class Order(models.Model):
    """A customer order created in our system (v2)."""
    NON_CONFIRMEE   = "non_confirmee"
    CONFIRMEE       = "confirmee"
    INJOIGNABLE     = "injoignable"
    PAS_SERIEUX     = "pas_serieux"
    RAPPELER        = "rappeler_plus_tard"
    EN_COURS        = "en_cours"          # Navex: colis en cours de livraison
    AU_MAGASIN      = "au_magasin"        # Navex: colis revenu au magasin/dépôt
    RETURNING       = "returning"         # En retour: Navex "Retour Expéditeur" or "Rtn client/agence"
    RETURNED        = "returned"          # Retourné (final): set when the return is scanned in v1
    LIVREE          = "livree"            # Navex delivered the colis
    PAYEE           = "payee"             # Payée: v1 ShippingOrder marked PAID (Payer matched/tout)
    ANNULEE         = "annulee"
    SUPPRIME_NAVEX  = "supprime_navex"  # Navex deleted the colis after our push

    STATUS_CHOICES = [
        (NON_CONFIRMEE,  "Non confirmée"),
        (CONFIRMEE,      "Confirmée"),
        (INJOIGNABLE,    "Injoignable"),
        (PAS_SERIEUX,    "Pas sérieux"),
        (RAPPELER,       "Rappeler plus tard"),
        (EN_COURS,       "En cours"),
        (AU_MAGASIN,     "Au magasin"),
        (RETURNING,      "En retour"),
        (RETURNED,       "Retourné"),
        (LIVREE,         "Livrée"),
        (PAYEE,          "Payée"),
        (ANNULEE,        "Annulée"),
        (SUPPRIME_NAVEX, "Supprimé Navex"),
    ]

    # Cancellation reasons (only meaningful when status == ANNULEE)
    CANCEL_CLIENT     = "client"
    CANCEL_CHANGEMENT = "changement"
    CANCEL_RUPTURE    = "rupture_stock"
    CANCEL_REASON_CHOICES = [
        ("",                "—"),
        (CANCEL_CLIENT,     "Client a annulé"),
        (CANCEL_CHANGEMENT, "Annulé pour changement"),
        (CANCEL_RUPTURE,    "Rupture de stock"),
    ]

    SOURCE_WEBFORM = "web_form"
    SOURCE_SHOPIFY = "shopify"
    SOURCE_CONVERTY = "converty"

    SOURCE_CHOICES = [
        (SOURCE_WEBFORM, "Saisie manuelle"),
        (SOURCE_SHOPIFY, "Shopify"),
        (SOURCE_CONVERTY, "Converty"),
    ]

    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="orders")
    # Per-order recipient name. The same person (identified by phone, on the
    # Customer) may place orders under different names, so the name belongs to
    # the order, not the shared Customer. Falls back to the customer's name when
    # blank (e.g. legacy orders created before this field existed).
    customer_name = models.CharField(max_length=200, blank=True, default="")
    sales_page = models.ForeignKey(SalesPage, on_delete=models.SET_NULL, null=True, blank=True, related_name="orders")
    region = models.ForeignKey(Region, on_delete=models.SET_NULL, null=True, blank=True, related_name="orders")
    ville = models.CharField(max_length=120, blank=True, default="")
    localite = models.CharField(max_length=120, blank=True, default="")
    address = models.TextField(blank=True, default="")

    delivery_fee = models.DecimalField(max_digits=10, decimal_places=3, default=7)
    discount = models.DecimalField(max_digits=10, decimal_places=3, default=0)
    total = models.DecimalField(max_digits=10, decimal_places=3, default=0,
        help_text="Computed: sum(line totals) + delivery_fee − discount")
    # Manually-set total (e.g. office grants a reduction after shipping, agreed
    # by phone with Navex). When set, this IS the order total and recalc_total()
    # will not recompute it — keeps "notre total" matching what Navex collects.
    price_override = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)

    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default=NON_CONFIRMEE)
    cancel_reason = models.CharField(max_length=30, choices=CANCEL_REASON_CHOICES, blank=True, default="",
        help_text="Raison de l'annulation (client / changement / rupture stock)")
    cancelled_at = models.DateTimeField(null=True, blank=True)
    # When the order became "livrée" (set once by the Navex sync on the LIVREE
    # transition, or by the pay flow). Used for delivered-per-day metrics.
    delivered_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")

    # Filled later by Phase 5 (Navex push) — blank means "not yet pushed"
    bordereau_barcode = models.CharField(max_length=50, blank=True, default="")
    pushed_to_navex_at = models.DateTimeField(null=True, blank=True)
    navex_label_url = models.URLField(max_length=500, blank=True, default="",
        help_text="Print URL returned by Navex after successful push")

    # Phase 7: cached Navex status (synced hourly via cron + on demand)
    navex_last_status = models.CharField(max_length=80, blank=True, default="",
        help_text="Statut Navex tel que retourné par leur API (ex: 'En cours', 'Livré', 'En magasin')")
    navex_last_synced_at = models.DateTimeField(null=True, blank=True)
    navex_last_status_raw = models.TextField(blank=True, default="",
        help_text="Réponse Navex complète au dernier sync (pour debug)")
    navex_motif = models.CharField(max_length=200, blank=True, default="",
        help_text="Motif lié au statut Navex actuel (ex: 'Client non sérieux')")
    navex_pre_etat = models.CharField(max_length=80, blank=True, default="",
        help_text="État précédent côté Navex")
    navex_livreur = models.CharField(max_length=120, blank=True, default="",
        help_text="Nom du livreur / agence Navex")
    navex_livreur_tel = models.CharField(max_length=30, blank=True, default="")

    # SMS notification dedup flags — each customer SMS fires once per order.
    sms_created_sent     = models.BooleanField(default=False)
    sms_injoignable_sent = models.BooleanField(default=False)
    sms_expedie_sent     = models.BooleanField(default=False)
    sms_en_cours_sent    = models.BooleanField(default=False)

    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default=SOURCE_WEBFORM)
    # Converty order _id (MongoDB ObjectId), used to push status changes back
    # to Converty (confirmed / rejected / delivered) and to dedupe webhooks.
    converty_order_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    # ---- DM conversation (Messenger) -------------------------------------
    # Snapshot of the customer's chat, used by the team to verify a DM order
    # without leaving the app. Bulky text is PURGED after 10 days by a cron
    # (the customer's PSID lives on Customer and is kept). If the customer
    # messages again, n8n/webhook re-attaches a fresh conversation here.
    conversation_text = models.TextField(blank=True, default="",
        help_text="Conversation Messenger capturée pour vérifier la commande. Supprimée après 10 jours.")
    conversation_updated_at = models.DateTimeField(null=True, blank=True,
        help_text="Date de la dernière capture/mise à jour de la conversation.")
    created_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="orders_created")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # Optional "schedule for later" date. Defaults to today on creation.
    # Used to hide orders that should be processed on a future day.
    scheduled_for = models.DateField(null=True, blank=True, db_index=True,
        help_text="Date à laquelle traiter la commande. NULL = pas de planification (= aujourd'hui).")

    # ---- Exchange fields -------------------------------------------------
    # If this order is an exchange of a previously-delivered order, this points
    # to the original Order. The original keeps its status. This new Order's
    # designation contains the NEW products going to the customer.
    exchange_of = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="exchanges",
        help_text="Commande livrée d'origine, si celle-ci est un échange.",
    )

    # Money actually collected, mirrored from the linked v1 ShippingOrder when
    # payment is confirmed via "Payer matched" / "Payer tout" (Navex prix).
    # Distinct from `total` (the computed order value) — this reflects what was
    # really collected. NULL until a payment is confirmed.
    amount_collected     = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True,
        help_text="Montant réellement encaissé (synchronisé depuis v1 lors du paiement Navex).")

    # Free-text reason captured when a non-confirmée order is set to
    # injoignable / rappeler / pas sérieux — so the reason is known later.
    status_note          = models.CharField(max_length=300, blank=True, default="",
        help_text="Note/raison saisie lors d'un changement de statut (injoignable, rappeler, pas sérieux).")
    # When the note was last written/updated, so the UI can show its time.
    status_note_at       = models.DateTimeField(null=True, blank=True)

    # When Navex processes an exchange, it generates a SECOND barcode for the
    # return colis (the one that will pick up the old products). We store it here.
    navex_return_barcode = models.CharField(max_length=80, blank=True, default="",
        help_text="Barcode du colis de retour (généré par Navex pour les échanges).")

    # Whether the exchange is due to our mistake (0 DT shipping) or
    # the client's change of mind (7 DT shipping fees).
    EXCHANGE_FAULT_NONE  = "none"
    EXCHANGE_FAULT_OURS  = "ours"
    EXCHANGE_FAULT_CLIENT = "client"
    EXCHANGE_FAULT_CHOICES = [
        (EXCHANGE_FAULT_NONE,  "—"),
        (EXCHANGE_FAULT_OURS,  "Notre faute"),
        (EXCHANGE_FAULT_CLIENT, "Faute client"),
    ]
    exchange_fault = models.CharField(
        max_length=10, choices=EXCHANGE_FAULT_CHOICES, default=EXCHANGE_FAULT_NONE,
        help_text="Pour les échanges : qui est en faute. Affecte les frais de livraison.",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["-created_at"]),
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["bordereau_barcode"]),
        ]

    def __str__(self):
        return f"#{self.id} — {self.customer} — {self.get_status_display()}"

    @property
    def display_name(self):
        """Per-order name if set, otherwise the customer's name."""
        return (self.customer_name or "").strip() or (self.customer.name if self.customer else "")

    def recalc_total(self):
        """Recompute total from order_offers + standalone lines + delivery − discount.
        Saves.

        EXCEPTION: for exchange orders (exchange_of != NULL), articles are NOT
        re-charged (the customer already paid for them on the original delivered
        order). Total = delivery_fee - discount only.
        """
        from decimal import Decimal
        # If a manual price override is set (office reduction), it IS the total.
        if self.price_override is not None:
            self.total = self.price_override
            self.save(update_fields=["total", "updated_at"])
            return
        if self.exchange_of_id:
            # Exchange: only the delivery fee, no articles re-charged
            self.total = max(
                Decimal("0"),
                (self.delivery_fee or 0) - (self.discount or 0),
            )
        else:
            offers_sum = sum((oo.offer_total for oo in self.order_offers.all()), Decimal("0"))
            # Standalone lines (lines without an OrderOffer parent) — for legacy/direct entry
            standalone_lines_sum = sum(
                (l.line_total for l in self.lines.filter(order_offer__isnull=True)),
                Decimal("0")
            )
            self.total = max(
                Decimal("0"),
                offers_sum + standalone_lines_sum + (self.delivery_fee or 0) - (self.discount or 0),
            )
        self.save(update_fields=["total", "updated_at"])

    @property
    def dm_platform(self):
        """Return 'instagram' or 'messenger' if this order came from a DM
        conversation, else ''. Used to show the right logo on the 💬 button.
        Cached per instance to avoid repeat queries."""
        if hasattr(self, "_dm_platform_cache"):
            return self._dm_platform_cache
        val = ""
        try:
            from .models import MessengerConversation
            conv = (MessengerConversation.objects
                    .filter(pending_order_id=self.id)
                    .order_by("-id").only("platform").first())
            if conv:
                val = conv.platform or "messenger"
        except Exception:
            val = ""
        self._dm_platform_cache = val
        return val

    @property
    def is_navex_delivered(self):
        """True if Navex shows this order as delivered (paid or otherwise).
        Used to gate the "Create Exchange" feature.
        """
        if not self.bordereau_barcode:
            return False
        s = (self.navex_last_status or "").strip().lower()
        return s in ("livre", "livré", "livrée", "livre paye", "livré payé", "livre payé", "livrer paye")

    @property
    def is_shipping_closed(self):
        """True if at least one linked ShippingOrder is in a 'closed' state
        (= all units scanned and order finalised). Used in /sales-orders/
        to show a green ✓ next to commands that have been fully scanned.
        """
        for so in self.shipping_orders.all():
            if so.status in ShippingOrder.CLOSED_STATUSES:
                return True
        return False

    @property
    def article_summary(self):
        """Text summary for list display: shows the PRODUCTS inside each offer
        with their colour and size (not just the offer name), plus any
        standalone product lines."""
        def _line_label(line):
            bits = [line.product.name]
            detail = []
            if line.variant and line.variant.color_label:
                detail.append(line.variant.color_label)
            if line.size:
                detail.append(line.size)
            label = line.product.name
            if detail:
                label += " (" + ", ".join(detail) + ")"
            if line.quantity and line.quantity > 1:
                label = f"{line.quantity}× " + label
            return label

        parts = []
        # Offer lines (products that belong to an offer)
        for oo in self.order_offers.all():
            for line in oo.lines.all():
                parts.append(_line_label(line))
        # Standalone product lines (not part of any offer)
        for line in self.lines.filter(order_offer__isnull=True):
            parts.append(_line_label(line))

        if not parts:
            return "—"
        return ", ".join(parts)


class OrderLine(models.Model):
    """One product line inside an Order. Stores price as snapshot."""
    order    = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="lines")
    product  = models.ForeignKey(Product, on_delete=models.PROTECT)
    variant  = models.ForeignKey(ProductVariant, on_delete=models.PROTECT, null=True, blank=True)
    size     = models.CharField(max_length=10, blank=True, default="")
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=3, default=0,
        help_text="Snapshot of the product's sell_price at order creation")

    # Phase-4b: lines can now belong to an OrderOffer instead of (or in addition to)
    # being directly attached to the Order. Nullable for backward compat.
    order_offer = models.ForeignKey(
        "OrderOffer", on_delete=models.CASCADE,
        null=True, blank=True, related_name="lines",
    )

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.quantity}× {self.product.name}"

    @property
    def line_total(self):
        from decimal import Decimal
        return (self.unit_price or Decimal("0")) * self.quantity


# ---------------------------------------------------------------------------
# OFFERS — bundles of products with a fixed bundle price.
# An offer can appear on multiple SalesPages (M2M).
# When picked in an order, the worker chooses variant + size per product.
# ---------------------------------------------------------------------------
class Offer(models.Model):
    name = models.CharField(max_length=120, unique=True)
    bundle_price = models.DecimalField(max_digits=10, decimal_places=3, default=0)
    is_active = models.BooleanField(default=True)
    sales_pages = models.ManyToManyField(SalesPage, related_name="offers", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.bundle_price} DT)"

    def price_for_page(self, page):
        """Return the price for this offer on a given SalesPage. Falls back to
        bundle_price when no page-specific override exists."""
        if page is not None:
            pp = self.page_prices.filter(sales_page=page).first()
            if pp is not None and pp.price is not None:
                return pp.price
        return self.bundle_price

    def price_for_page_name(self, page_name):
        if page_name:
            pp = self.page_prices.filter(sales_page__name__iexact=page_name).first()
            if pp is not None and pp.price is not None:
                return pp.price
        return self.bundle_price


class OfferPagePrice(models.Model):
    """Per-page price override for an offer. Lets the same offer have a
    different price depending on which sales page it's sold on."""
    offer = models.ForeignKey(Offer, on_delete=models.CASCADE, related_name="page_prices")
    sales_page = models.ForeignKey(SalesPage, on_delete=models.CASCADE, related_name="offer_prices")
    price = models.DecimalField(max_digits=10, decimal_places=3, default=0)

    class Meta:
        unique_together = (("offer", "sales_page"),)

    def __str__(self):
        return f"{self.offer.name} @ {self.sales_page.name}: {self.price} DT"


class OfferProduct(models.Model):
    """A product that belongs to an offer. Quantity defaults to 1.
    Variant and size are NOT pinned here — picked at order-creation time."""
    offer    = models.ForeignKey(Offer, on_delete=models.CASCADE, related_name="products")
    product  = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.quantity}× {self.product.name} (in offer {self.offer.name})"


# ---------------------------------------------------------------------------
# ADS — Meta/Facebook ad campaigns synced from the Google Sheet (n8n fills it).
# Each ad links to ONE offer; an offer can have MANY ads. Spend is synced from
# the sheet; the offer link is set manually in the ads interface.
# ---------------------------------------------------------------------------
class Ad(models.Model):
    # attribution kind: 'offer' = linked to 1-2 specific offers (Converty/FB),
    # 'barats' = part of the barats.tn carousel pool (spend blended over ALL
    # barats.tn orders, not any single offer).
    ATTR_OFFER = "offer"
    ATTR_BARATS = "barats"
    ATTR_CHOICES = [(ATTR_OFFER, "Offre(s) liée(s)"), (ATTR_BARATS, "Carrousel Barats.tn")]

    # Meta campaign id — the STABLE key. Names can be renamed in Ads Manager,
    # so we sync/match on this id and only display the (latest) name.
    campaign_id = models.CharField(max_length=64, unique=True, null=True, blank=True,
        help_text="ID de campagne Meta (clé stable de synchronisation).")
    # campaign_name is now just a display label, refreshed on each sync.
    campaign_name = models.CharField(max_length=200, db_index=True,
        help_text="Nom de la campagne (affichage ; peut changer).")
    spend = models.DecimalField(max_digits=12, decimal_places=2, default=0,
        help_text="Dépense convertie en TND (dinars).")
    # Original (pre-conversion) spend and its account/currency, so the dashboard
    # can show both the account's native amount (EUR/USD) and the TND value.
    spend_original = models.DecimalField(max_digits=12, decimal_places=2, default=0,
        help_text="Dépense dans la devise d'origine du compte.")
    account_id = models.CharField(max_length=64, blank=True, default="",
        help_text="Compte publicitaire Meta d'où vient cette pub.")
    currency = models.CharField(max_length=8, blank=True, default="",
        help_text="Devise d'origine du compte (EUR, USD, TND…).")
    archived = models.BooleanField(default=False,
        help_text="Pub annulée/désactivée dans Meta : masquée du dashboard du "
                  "jour et exclue de l'attribution (l'historique passé reste).")
    effective_status = models.CharField(max_length=32, blank=True, default="",
        help_text="Statut Meta de la campagne (ACTIVE, PAUSED, DELETED…), "
                  "rafraîchi à chaque sync.")
    attribution = models.CharField(max_length=10, choices=ATTR_CHOICES, default=ATTR_OFFER,
        help_text="Comment cette pub est attribuée : à des offres précises, ou au pool Barats.tn.")
    # Legacy single link — kept for back-compat. New code uses `offers` (M2M).
    offer = models.ForeignKey(Offer, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="ads",
        help_text="(Ancien) Offre unique liée. Utiliser plutôt 'offers'.")
    # Converty / Facebook ads link to ONE or TWO (offer, page) pairs. The page
    # matters: the same offer (e.g. Ensemble ICY MAZE) is sold on several pages
    # and each page has its OWN ad. Linking through AdOfferLink lets us match
    # orders on BOTH offer AND sales_page. Spend is pooled across the ad's links.
    offers = models.ManyToManyField(Offer, through="AdOfferLink",
        related_name="linked_ads", blank=True,
        help_text="1 ou 2 paires (offre, page) liées à cette pub.")
    last_synced_at = models.DateTimeField(null=True, blank=True,
        help_text="Dernière synchronisation de la dépense depuis le Sheet.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-spend"]

    def __str__(self):
        return f"{self.campaign_name} ({self.spend})"


class AdOfferLink(models.Model):
    """One (offer, page) pair covered by an ad. An ad may have 1 or 2 of these.
    Orders are attributed to the ad only when they match BOTH the offer and the
    sales page, so the same offer on different pages goes to different ads."""
    ad = models.ForeignKey(Ad, on_delete=models.CASCADE, related_name="links")
    offer = models.ForeignKey(Offer, on_delete=models.CASCADE, related_name="ad_links")
    sales_page = models.ForeignKey(SalesPage, on_delete=models.CASCADE,
        related_name="ad_links", null=True, blank=True,
        help_text="Page de vente. Vide = toutes pages (rare).")

    class Meta:
        unique_together = (("ad", "offer", "sales_page"),)

    def __str__(self):
        pg = self.sales_page.name if self.sales_page else "toutes pages"
        return f"{self.ad.campaign_name} → {self.offer.name} @ {pg}"


# ---------------------------------------------------------------------------
# ORDER OFFERS — when an order picks an offer, this row stores
# the snapshot of the offer + its bundle price. The lines (one per product
# inside the offer with chosen variant/size) link to it via OrderLine.order_offer.
# ---------------------------------------------------------------------------
class OrderOffer(models.Model):
    order        = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="order_offers")
    offer        = models.ForeignKey(Offer, on_delete=models.PROTECT, null=True, blank=True)
    offer_name   = models.CharField(max_length=120, blank=True, default="",
        help_text="Snapshot of offer name in case the offer is renamed/deleted later")
    bundle_price = models.DecimalField(max_digits=10, decimal_places=3, default=0)
    quantity     = models.PositiveIntegerField(default=1,
        help_text="How many copies of this offer in the order")

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.quantity}× {self.offer_name or '?'}  in order #{self.order_id}"

    @property
    def offer_total(self):
        from decimal import Decimal
        return (self.bundle_price or Decimal("0")) * self.quantity


# ---------------------------------------------------------------------------
# USER ROLES — non-superuser access scoping
# Each User has one Profile with a role: shipping, office, or messages.
# Superusers bypass roles entirely (see all features).
# ---------------------------------------------------------------------------
class UserProfile(models.Model):
    SHIPPING = "shipping"
    OFFICE   = "office"
    MESSAGES = "messages"

    ROLE_CHOICES = [
        (SHIPPING, "Shipping"),
        (OFFICE,   "Office"),
        (MESSAGES, "Messages Team"),
    ]

    # UI theme preferences
    THEME_DARK  = "dark"
    THEME_LIGHT = "light"
    THEME_CHOICES = [
        (THEME_DARK,  "Sombre"),
        (THEME_LIGHT, "Clair"),
    ]

    user = models.OneToOneField(
        "auth.User", on_delete=models.CASCADE, related_name="profile",
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default=OFFICE)
    theme = models.CharField(max_length=10, choices=THEME_CHOICES, default=THEME_DARK)

    def __str__(self):
        return f"{self.user.username} — {self.get_role_display()}"


# ---------------------------------------------------------------------------
# AUDIT LOG — records who did what, when.
# Additive only. Every meaningful action writes one row here.
# ---------------------------------------------------------------------------
class ExchangeReturnItem(models.Model):
    """Articles that the customer is returning as part of an Exchange order.

    Each row is one physical ProductUnit (or one variant+size if we don't know
    which specific unit) that is expected to come back from the customer.
    Linked to the EXCHANGE order (not the original delivered order).
    """
    RECEIVED_PENDING = "pending"  # Waiting for the return colis to arrive
    RECEIVED_OK      = "received"  # We got the unit back, it's in stock again
    RECEIVED_MISSING = "missing"   # Customer didn't include it / lost

    STATUS_CHOICES = [
        (RECEIVED_PENDING, "En attente"),
        (RECEIVED_OK,      "Reçu"),
        (RECEIVED_MISSING, "Manquant"),
    ]

    exchange_order = models.ForeignKey(
        "Order", on_delete=models.CASCADE, related_name="return_items",
        help_text="L'Order qui est l'échange (pas la commande originale livrée).",
    )
    # Either point to a specific physical unit (preferred) OR to a variant
    # if we just know "client returns 1 Pull Blueline gris M" without a specific unit
    unit = models.ForeignKey(
        "ProductUnit", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="exchange_returns",
    )
    variant = models.ForeignKey(
        "ProductVariant", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="exchange_returns",
    )
    size = models.CharField(max_length=20, blank=True, default="")
    product_name_snapshot = models.CharField(max_length=200, blank=True, default="",
        help_text="Name at time of return creation, for display.")
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=RECEIVED_PENDING,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["exchange_order", "status"]),
        ]

    def __str__(self):
        return f"Return for Order #{self.exchange_order_id}: {self.product_name_snapshot} ({self.size})"


class AuditLog(models.Model):
    """One row per significant action — login, scan, edit, status change, etc."""
    # Action categories. Add new ones over time.
    LOGIN          = "login"
    LOGOUT         = "logout"
    SCAN_SHIPPING  = "scan_shipping"
    SCAN_RETURN    = "scan_return"
    SCAN_RECEPTION = "scan_reception"
    STATUS_CHANGE  = "status_change"
    EDIT           = "edit"
    CREATE         = "create"
    DELETE         = "delete"
    NAVEX_PUSH     = "navex_push"
    NAVEX_SYNC     = "navex_sync"
    PAYMENT        = "payment"
    OTHER          = "other"

    ACTION_CHOICES = [
        (LOGIN,          "Connexion"),
        (LOGOUT,         "Déconnexion"),
        (SCAN_SHIPPING,  "Scan expédition"),
        (SCAN_RETURN,    "Scan retour"),
        (SCAN_RECEPTION, "Scan réception"),
        (STATUS_CHANGE,  "Changement de statut"),
        (EDIT,           "Modification"),
        (CREATE,         "Création"),
        (DELETE,         "Suppression"),
        (NAVEX_PUSH,     "Push Navex"),
        (NAVEX_SYNC,     "Sync Navex"),
        (PAYMENT,        "Paiement"),
        (OTHER,          "Autre"),
    ]

    # Who did it — nullable so login/logout signals can record even when user FK is unstable.
    # SET_NULL means: if a user is ever deleted, the log row stays but the link goes to NULL.
    # username is also stored as plain text so we never lose attribution.
    user = models.ForeignKey(
        "auth.User", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="audit_logs",
    )
    username = models.CharField(max_length=150, blank=True, default="")

    # What happened
    action = models.CharField(max_length=30, choices=ACTION_CHOICES, default=OTHER)
    description = models.CharField(max_length=500, blank=True, default="")

    # Optional links to the thing affected (all nullable, all SET_NULL on delete)
    target_unit_barcode = models.CharField(max_length=100, blank=True, default="")
    target_order_barcode = models.CharField(max_length=100, blank=True, default="")
    target_model = models.CharField(max_length=80, blank=True, default="")
    target_id = models.CharField(max_length=50, blank=True, default="")

    # Free-form payload for diff / before / after — JSON text
    extra = models.TextField(blank=True, default="")

    # Network info
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["-created_at"]),
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["action", "-created_at"]),
        ]

    def __str__(self):
        who = self.username or "?"
        return f"{who} — {self.get_action_display()} @ {self.created_at:%Y-%m-%d %H:%M}"


def log_action(user, action, description="", request=None, **kwargs):
    """Helper to write an audit row safely. Never crashes the caller.

    Usage:
        from inventory.models import log_action, AuditLog
        log_action(request.user, AuditLog.SCAN_SHIPPING,
                   description=f"Scanned {barcode} into order {order_bc}",
                   request=request,
                   target_unit_barcode=barcode,
                   target_order_barcode=order_bc)
    """
    try:
        ip = None
        if request is not None:
            ip = request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip() or \
                 request.META.get("REMOTE_ADDR") or None
        AuditLog.objects.create(
            user=user if (user and user.is_authenticated) else None,
            username=(user.username if user and user.is_authenticated else "anonymous"),
            action=action,
            description=description[:500],
            ip_address=ip,
            target_unit_barcode=kwargs.get("target_unit_barcode", "")[:100],
            target_order_barcode=kwargs.get("target_order_barcode", "")[:100],
            target_model=kwargs.get("target_model", "")[:80],
            target_id=str(kwargs.get("target_id", ""))[:50],
            extra=kwargs.get("extra", "")[:5000],
        )
    except Exception:
        # Audit failures must never break the user's action.
        pass


class ConvertyConnection(models.Model):
    """Stores the OAuth tokens for the seller's Converty store. There is a
    single active connection (one store). client_id / client_secret come from
    environment variables — only the per-store tokens live here.
    """
    store_id        = models.CharField(max_length=64, blank=True, default="")
    store_name      = models.CharField(max_length=200, blank=True, default="")
    store_currency  = models.CharField(max_length=10, blank=True, default="")
    access_token    = models.TextField(blank=True, default="")
    refresh_token   = models.TextField(blank=True, default="")
    access_token_expires_at = models.DateTimeField(null=True, blank=True)
    is_active       = models.BooleanField(default=True)
    created_at      = models.DateTimeField(auto_now_add=True)
    updated_at      = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Converty: {self.store_name or self.store_id or 'non connecté'}"


class CustomerHistory(models.Model):
    """Historic delivery/return stats per phone number, seeded from Navex
    exports (orders that predate this system). Combined with live in-system
    orders to show a customer's full track record (good vs bad client).

    'annulé' outcomes are intentionally NOT counted here.
    """
    phone              = models.CharField(max_length=20, unique=True, db_index=True)
    historic_total     = models.IntegerField(default=0)   # total historic orders seen
    historic_delivered = models.IntegerField(default=0)   # livré / payé
    historic_returned  = models.IntegerField(default=0)   # real retour (refusal/no-show)
    updated_at         = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.phone}: {self.historic_delivered} livré / {self.historic_returned} retour"


class MessengerConversation(models.Model):
    """An incoming Messenger/Instagram conversation captured via the Meta
    webhook. Stores the raw messages, the ad the chat came from (attribution),
    the Gemini-extracted order data, and a link to the pending Order created
    for human confirmation.

    Lifecycle:
      new        → message(s) received, not yet extracted
      extracted  → Gemini parsed an order; pending Order created
      confirmed  → a human validated it into a real order
      ignored    → not an order / spam / dismissed
    """
    NEW       = "new"
    EXTRACTED = "extracted"
    CONFIRMED = "confirmed"
    IGNORED   = "ignored"
    STATUS_CHOICES = [
        (NEW,       "Nouveau"),
        (EXTRACTED, "Extrait"),
        (CONFIRMED, "Confirmé"),
        (IGNORED,   "Ignoré"),
    ]

    # Meta identifiers
    platform        = models.CharField(max_length=20, default="messenger",
        help_text="messenger / instagram")
    page_id         = models.CharField(max_length=64, blank=True, default="")
    sender_id       = models.CharField(max_length=64, db_index=True,
        help_text="PSID (page-scoped user id) of the customer.")
    sender_name     = models.CharField(max_length=200, blank=True, default="")

    # Conversation content — appended as messages arrive (JSON list of
    # {"from": "user"|"page", "text": "...", "ts": "..."}).
    messages        = models.JSONField(default=list, blank=True)

    # Ad attribution captured from the referral on the first message.
    source_ad_id        = models.CharField(max_length=120, blank=True, default="")
    source_ad_ref       = models.CharField(max_length=200, blank=True, default="")
    source_campaign     = models.CharField(max_length=200, blank=True, default="")
    source_campaign_name = models.CharField(max_length=200, blank=True, default="",
        help_text="Vrai nom de la campagne Meta, résolu depuis l'ad_id du referral.")
    ctwa_clid           = models.CharField(max_length=200, blank=True, default="")
    matched_ad      = models.ForeignKey("Ad", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="conversations")

    # Gemini extraction result (raw JSON) + the pending order it produced.
    extracted       = models.JSONField(null=True, blank=True)
    pending_order   = models.ForeignKey("Order", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="messenger_conversations")

    auto_replied    = models.BooleanField(default=False,
        help_text="Whether the one-time auto-reply was already sent.")
    gemini_enriched = models.BooleanField(default=False,
        help_text="Whether the deferred Gemini address/product pass has run.")
    status          = models.CharField(max_length=20, choices=STATUS_CHOICES,
        default=NEW, db_index=True)
    created_at      = models.DateTimeField(auto_now_add=True)
    updated_at      = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return f"{self.platform}:{self.sender_name or self.sender_id} ({self.status})"

    @property
    def last_message_text(self):
        for m in reversed(self.messages or []):
            if m.get("from") == "user" and m.get("text"):
                return m["text"]
        return ""
