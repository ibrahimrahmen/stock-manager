from django.contrib import admin
from .models import (
    Product, ProductVariant, ProductUnit,
    ShippingOrder, OrderItem, StockMovement,
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
