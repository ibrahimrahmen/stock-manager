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
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({self.code})"

    @property
    def total_stock(self):
        return ProductUnit.objects.filter(
            variant__product=self,
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

    @property
    def stock_by_size(self):
        sizes = {}
        for unit in self.units.filter(status__in=(ProductUnit.IN_STOCK, ProductUnit.RETURNED)):
            sizes[unit.size] = sizes.get(unit.size, 0) + 1
        return sizes

    @property
    def total_stock(self):
        return self.units.filter(status__in=(ProductUnit.IN_STOCK, ProductUnit.RETURNED)).count()


class ProductUnit(models.Model):
    IN_STOCK  = "in_stock"
    SHIPPED   = "shipped"
    PAID      = "paid"
    RETURNED  = "returned"

    STATUS_CHOICES = [
        (IN_STOCK,  "En stock"),
        (SHIPPED,   "Expédié"),
        (PAID,      "Payé"),
        (RETURNED,  "Retourné"),
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
    amount_collected     = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True,
                                               help_text="Actual amount collected from client")
    notes                = models.TextField(blank=True)
    # Client info from Navex
    client_name       = models.CharField(max_length=200, blank=True, default="")
    client_phone      = models.CharField(max_length=50, blank=True, default="")
    client_address    = models.CharField(max_length=500, blank=True, default="")
    client_ville      = models.CharField(max_length=100, blank=True, default="")
    navex_designation = models.CharField(max_length=500, blank=True, default="")

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
    RECEIVED = "received"
    SHIPPED  = "shipped"
    PAID     = "paid"
    RETURNED = "returned"

    TYPE_CHOICES = [
        (RECEIVED, "Réception"),
        (SHIPPED,  "Expédition"),
        (PAID,     "Payé"),
        (RETURNED, "Retour"),
    ]

    unit          = models.ForeignKey(ProductUnit, on_delete=models.PROTECT, related_name="movements")
    movement_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    reference     = models.CharField(max_length=255, blank=True)
    moved_at      = models.DateTimeField(auto_now_add=True)

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
        return self.current_stock < self.threshold


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
