from django.contrib import admin
from .models import (
    Product, ProductVariant, ProductUnit,
    ShippingOrder, OrderItem, StockMovement, Payment, SizeAlert,
    SalesPage, Customer,
)


class ProductVariantInline(admin.TabularInline):
    model = ProductVariant
    extra = 1
    fields = ("color_name", "color_label", "image")


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "category", "buy_price", "sell_price", "total_stock", "alert_threshold")
    search_fields = ("name", "code", "category")
    inlines = [ProductVariantInline]

    def total_stock(self, obj):
        return obj.total_stock
    total_stock.short_description = "Stock total"


@admin.register(ProductVariant)
class ProductVariantAdmin(admin.ModelAdmin):
    list_display = ("product", "color_label", "color_name", "total_stock")
    list_filter = ("product",)
    search_fields = ("product__name", "color_name", "color_label")

    def total_stock(self, obj):
        return obj.total_stock
    total_stock.short_description = "Stock"


@admin.register(ProductUnit)
class ProductUnitAdmin(admin.ModelAdmin):
    list_display = ("barcode", "product_name", "color_label", "size", "status", "created_at")
    list_filter = ("status", "size", "variant__product")
    search_fields = ("barcode", "variant__product__name", "variant__color_name")
    readonly_fields = ("created_at", "updated_at")

    def product_name(self, obj):
        return obj.variant.product.name
    product_name.short_description = "Produit"

    def color_label(self, obj):
        return obj.variant.color_label
    color_label.short_description = "Couleur"


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    readonly_fields = ("unit", "scanned_at")
    can_delete = False


@admin.register(ShippingOrder)
class ShippingOrderAdmin(admin.ModelAdmin):
    list_display = ("bordereau_barcode", "status", "unit_count", "opened_at", "closed_at")
    list_filter = ("status",)
    search_fields = ("bordereau_barcode",)
    readonly_fields = ("opened_at", "closed_at")
    inlines = [OrderItemInline]

    def unit_count(self, obj):
        return obj.unit_count
    unit_count.short_description = "Unités"


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    list_display = ("unit", "movement_type", "reference", "moved_at")
    list_filter = ("movement_type",)
    search_fields = ("unit__barcode", "reference")
    readonly_fields = ("moved_at",)


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("order", "amount_collected", "amount_expected", "created_at")

@admin.register(OrderItem)
class OrderItemAdmin(admin.ModelAdmin):
    list_display = ("order", "unit", "scanned_at")
    list_filter = ("order__status",)


@admin.register(SizeAlert)
class SizeAlertAdmin(admin.ModelAdmin):
    list_display = ("variant", "size", "threshold", "current_stock", "is_triggered")
    list_filter = ("variant__product",)
    search_fields = ("variant__product__name", "size")

    def current_stock(self, obj):
        return obj.current_stock
    current_stock.short_description = "Stock actuel"

    def is_triggered(self, obj):
        return "⚠ OUI" if obj.is_triggered else "✓ OK"
    is_triggered.short_description = "Alerte"


# --- V2 (Phase 1) ---
@admin.register(SalesPage)
class SalesPageAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name",)
    readonly_fields = ("created_at",)


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("phone", "name", "created_at")
    search_fields = ("phone", "name")
    readonly_fields = ("created_at",)
