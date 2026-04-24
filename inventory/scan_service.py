"""
Status flow:
  in_stock → shipped → paid
                ↓
             returned  (counts as available stock, can be shipped again)

Rules:
  - Only in_stock or returned units can be added to a shipping order
  - returned cannot go directly to paid
  - Scan stock = create new unit as in_stock
  - Scan return = mark shipped/paid unit as returned
"""

from django.utils import timezone
from django.db import transaction

from .models import (
    Product, ProductVariant, ProductUnit,
    ShippingOrder, OrderItem, StockMovement,
)
from .barcode_parser import parse_barcode, is_bordereau_barcode


def handle_shipping_scan(barcode: str) -> dict:
    barcode = barcode.strip().upper()
    if is_bordereau_barcode(barcode):
        return _handle_bordereau(barcode)
    return _handle_unit_scan(barcode)


def _handle_bordereau(barcode: str) -> dict:
    closed_order = None
    with transaction.atomic():
        for order in ShippingOrder.objects.filter(status=ShippingOrder.OPEN):
            # Block closing an empty order
            if order.items.count() == 0:
                return {
                    "status": "error",
                    "message": f"Impossible de fermer l'ordre {order.bordereau_barcode} — aucune unité scannée ! Scannez au moins un produit avant de fermer.",
                    "code": "EMPTY_ORDER",
                }
            order.status = ShippingOrder.CLOSED
            order.closed_at = timezone.now()
            order.save()
            for item in order.items.select_related("unit"):
                item.unit.status = ProductUnit.SHIPPED
                item.unit.save()
                item.status_at_close = ProductUnit.SHIPPED
                item.save(update_fields=["status_at_close"])
                StockMovement.objects.create(
                    unit=item.unit, movement_type=StockMovement.SHIPPED,
                    reference=order.bordereau_barcode,
                )
            closed_order = order

        if ShippingOrder.objects.filter(bordereau_barcode=barcode).exists():
            return {"status": "error", "message": f"Ce bordereau ({barcode}) a déjà été utilisé.", "code": "BORDEREAU_DUPLICATE"}

        new_order = ShippingOrder.objects.create(bordereau_barcode=barcode, status=ShippingOrder.OPEN)

    response = {
        "status": "ok", "type": "bordereau",
        "new_order": {"id": new_order.id, "bordereau_barcode": new_order.bordereau_barcode},
    }
    if closed_order:
        response["closed_order"] = {"id": closed_order.id, "bordereau_barcode": closed_order.bordereau_barcode, "unit_count": closed_order.unit_count}
        response["message"] = f"Ordre {closed_order.bordereau_barcode} fermé ({closed_order.unit_count} unité(s)). Nouvel ordre : {barcode}"
    else:
        response["message"] = f"Nouvel ordre ouvert : {barcode}"
    return response


def _handle_unit_scan(barcode: str) -> dict:
    open_order = ShippingOrder.objects.filter(status=ShippingOrder.OPEN).first()
    if not open_order:
        return {"status": "error", "message": "Aucun ordre ouvert. Scannez d'abord un bordereau.", "code": "NO_OPEN_ORDER"}

    try:
        unit = ProductUnit.objects.select_related("variant__product").get(barcode=barcode)
    except ProductUnit.DoesNotExist:
        return {"status": "error", "message": f"Unité introuvable : {barcode}", "code": "UNIT_NOT_FOUND"}

    if OrderItem.objects.filter(order=open_order, unit=unit).exists():
        return {"status": "error", "message": f"{barcode} est déjà dans cet ordre.", "code": "ALREADY_IN_ORDER"}

    # in_stock and returned can both be shipped
    if unit.status not in (ProductUnit.IN_STOCK, ProductUnit.RETURNED):
        msgs = {
            ProductUnit.SHIPPED: "déjà expédié",
            ProductUnit.PAID:    "déjà payé",
        }
        return {"status": "error", "message": f"{barcode} ne peut pas être ajouté — {msgs.get(unit.status, unit.get_status_display())}.", "code": "INVALID_STATUS"}

    with transaction.atomic():
        OrderItem.objects.create(order=open_order, unit=unit, status_at_scan=ProductUnit.SHIPPED)
        unit.status = ProductUnit.SHIPPED
        unit.save()

    variant = unit.variant
    return {
        "status": "ok", "type": "unit",
        "message": f"{variant.product.name} {variant.color_label} — {unit.size} ajouté.",
        "unit": {
            "barcode": unit.barcode, "size": unit.size, "status": unit.status,
            "product_name": variant.product.name, "color_label": variant.color_label,
            "sell_price": str(variant.product.sell_price),
            "image_url": variant.image.url if variant.image else None,
        },
        "order": {"id": open_order.id, "bordereau_barcode": open_order.bordereau_barcode, "unit_count": open_order.items.count()},
    }


def handle_stock_scan(barcode: str) -> dict:
    """Add new stock — only creates new units, never modifies existing ones."""
    barcode = barcode.strip().upper()
    parsed = parse_barcode(barcode)

    if not parsed:
        return {"status": "error", "message": f"Format invalide : {barcode}. Attendu : CODE-COULEUR-TAILLE-NUM (ex: RLF-RED-40-001)", "code": "INVALID_FORMAT"}

    if ProductUnit.objects.filter(barcode=barcode).exists():
        return {"status": "error", "message": f"Ce barcode existe déjà en base.", "code": "DUPLICATE_BARCODE"}

    try:
        product = Product.objects.get(code=parsed.product_code)
    except Product.DoesNotExist:
        return {"status": "error", "message": f"Produit inconnu : {parsed.product_code}. Créez-le dans l'admin.", "code": "PRODUCT_NOT_FOUND"}

    try:
        variant = ProductVariant.objects.get(product=product, color_name=parsed.color_name)
    except ProductVariant.DoesNotExist:
        return {"status": "error", "message": f"Variante inconnue : {parsed.color_name} pour {parsed.product_code}.", "code": "VARIANT_NOT_FOUND"}

    with transaction.atomic():
        unit = ProductUnit.objects.create(variant=variant, barcode=barcode, size=parsed.size, status=ProductUnit.IN_STOCK)
        StockMovement.objects.create(unit=unit, movement_type=StockMovement.RECEIVED, reference="RECEPTION")

    return {
        "status": "ok", "type": "received",
        "message": f"{variant.product.name} {variant.color_label} taille {unit.size} ajouté au stock.",
        "unit": {
            "barcode": unit.barcode, "size": unit.size,
            "product_name": variant.product.name, "color_label": variant.color_label,
            "sell_price": str(variant.product.sell_price),
            "image_url": variant.image.url if variant.image else None,
        },
    }
