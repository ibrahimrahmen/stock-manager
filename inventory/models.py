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
    name = models.CharField(max_length=120, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.phone})" if self.name else self.phone


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

    user = models.OneToOneField(
        "auth.User", on_delete=models.CASCADE, related_name="profile",
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default=OFFICE)

    def __str__(self):
        return f"{self.user.username} — {self.get_role_display()}"


# ---------------------------------------------------------------------------
# AUDIT LOG — records who did what, when.
# Additive only. Every meaningful action writes one row here.
# ---------------------------------------------------------------------------
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


