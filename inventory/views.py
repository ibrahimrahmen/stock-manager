import json
from decimal import Decimal
from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse, HttpResponseForbidden
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.db import transaction
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required

from .models import (
    Product, ProductVariant, ProductUnit,
    ShippingOrder, OrderItem, StockMovement, Payment, SizeAlert, OrderVerification, ScanSessionLog,
)
from .scan_service import handle_shipping_scan, handle_stock_scan


# ---------------------------------------------------------------------------
# SCAN PAGES
# ---------------------------------------------------------------------------

@login_required(login_url="/login/")
def shipping_scan(request):
    open_order = ShippingOrder.objects.filter(status=ShippingOrder.OPEN).first()
    return render(request, "inventory/shipping_scan.html", {"open_order": open_order})

@login_required(login_url="/login/")
def reception_scan(request):
    return render(request, "inventory/reception_scan.html", {})

@login_required(login_url="/login/")
def return_scan(request):
    return render(request, "inventory/return_scan.html", {})

@login_required(login_url="/login/")
def payment_scan(request):
    return render(request, "inventory/payment_scan.html", {})

@login_required(login_url="/login/")
def stock_value(request):
    if not request.user.is_staff:
        return HttpResponseForbidden("Acces reserve aux administrateurs.")
    variants = ProductVariant.objects.select_related("product").prefetch_related("units")
    rows = []
    total_buy        = Decimal("0")
    total_sell       = Decimal("0")
    total_buy_shipped  = Decimal("0")
    total_sell_shipped = Decimal("0")

    OWNED_STATUSES = (
        ProductUnit.IN_STOCK,
        ProductUnit.SHIPPED,
        ProductUnit.SHIPPED,
        ProductUnit.RETURNED,
    )

    for variant in variants:
        in_stock       = variant.units.filter(status=ProductUnit.IN_STOCK).count()
        in_order       = variant.units.filter(status=ProductUnit.SHIPPED).count()
        shipped        = variant.units.filter(status=ProductUnit.SHIPPED).count()
        pending_return = variant.units.filter(status=ProductUnit.RETURNED).count()
        total_units    = in_stock + in_order + shipped + pending_return
        if total_units == 0:
            continue
        buy  = variant.product.buy_price  * total_units
        sell = variant.product.sell_price * total_units
        buy_shipped  = variant.product.buy_price  * (shipped + pending_return)
        sell_shipped = variant.product.sell_price * (shipped + pending_return)
        total_buy          += buy
        total_sell         += sell
        total_buy_shipped  += buy_shipped
        total_sell_shipped += sell_shipped
        rows.append({
            "variant": variant,
            "in_stock": in_stock,
            "shipped": in_order,
            "shipped": shipped,
            "returned": pending_return,
            "total_units": total_units,
            "buy_total": buy,
            "sell_total": sell,
        })

    return render(request, "inventory/stock_value.html", {
        "rows": rows,
        "total_buy": total_buy,
        "total_sell": total_sell,
        "potential_profit": total_sell - total_buy,
        "total_buy_shipped": total_buy_shipped,
        "total_sell_shipped": total_sell_shipped,
    })


# ---------------------------------------------------------------------------
# SCAN APIs
# ---------------------------------------------------------------------------

@csrf_exempt
@require_POST
def api_scan_shipping(request):
    data = json.loads(request.body)
    barcode = data.get("barcode", "").strip()
    if not barcode:
        return JsonResponse({"status": "error", "message": "Barcode vide."}, status=400)
    return JsonResponse(handle_shipping_scan(barcode))


@csrf_exempt
@require_POST
def api_scan_reception(request):
    data = json.loads(request.body)
    barcode = data.get("barcode", "").strip()
    if not barcode:
        return JsonResponse({"status": "error", "message": "Barcode vide."}, status=400)
    return JsonResponse(handle_stock_scan(barcode))


@csrf_exempt
@require_POST
def api_remove_from_order(request):
    """Remove a unit from the currently open order."""
    data = json.loads(request.body)
    barcode = data.get("barcode", "").strip().upper()
    open_order = ShippingOrder.objects.filter(status=ShippingOrder.OPEN).first()
    if not open_order:
        return JsonResponse({"status": "error", "message": "Aucun ordre ouvert."})
    try:
        unit = ProductUnit.objects.get(barcode=barcode)
        item = OrderItem.objects.get(order=open_order, unit=unit)
        item.delete()
        unit.status = ProductUnit.IN_STOCK
        unit.save()
        return JsonResponse({
            "status": "ok",
            "message": f"{barcode} retiré de l'ordre.",
            "unit_count": open_order.items.count(),
        })
    except (ProductUnit.DoesNotExist, OrderItem.DoesNotExist):
        return JsonResponse({"status": "error", "message": f"Unité {barcode} non trouvée dans cet ordre."})


def login_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        next_url = request.POST.get('next', '/')
        user = authenticate(request, username=username, password=password)
        if user:
            login(request, user)
            return redirect(next_url or 'dashboard')
        return render(request, 'inventory/login.html', {'form': type('F', (), {'errors': True})(), 'next': next_url})
    return render(request, 'inventory/login.html', {'next': request.GET.get('next', '/')})


def logout_view(request):
    logout(request)
    return redirect('login')



def _do_return_unit(unit):
    variant = unit.variant
    reconciliation = None
    order_item = unit.order_items.select_related("order").order_by("-scanned_at").first()
    if order_item and order_item.order.status == ShippingOrder.PAID:
        order = order_item.order
        refund_amount = variant.product.sell_price
        new_amount = max(Decimal("0"), (order.amount_collected or Decimal("0")) - refund_amount)
        order.amount_collected = new_amount
        order.save()
        try:
            payment = order.payment
            payment.amount_collected = new_amount
            payment.amount_expected = max(Decimal("0"), payment.amount_expected - refund_amount)
            payment.save()
        except Exception:
            pass
        reconciliation = {
            "order_bordereau": order.bordereau_barcode,
            "refund_amount": str(refund_amount),
            "new_order_amount": str(new_amount),
            "message": f"Ordre {order.bordereau_barcode} — montant ajuste de -{refund_amount} TND (nouveau total : {new_amount} TND)",
        }
    unit.status = ProductUnit.RETURNED
    unit.save()
    StockMovement.objects.create(unit=unit, movement_type=StockMovement.RETURNED, reference="RETOUR")
    if order_item:
        order_item.status_at_payment = ProductUnit.RETURNED
        order_item.save(update_fields=["status_at_payment"])
    # Update order status based on remaining units
    if order_item and order_item.order:
        _update_order_return_status(order_item.order)
    unit_data = {
        "barcode": unit.barcode, "size": unit.size,
        "product_name": variant.product.name, "color_label": variant.color_label,
        "sell_price": str(variant.product.sell_price),
        "image_url": variant.image.url if variant.image else None,
    }
    return unit_data, reconciliation


@csrf_exempt
@require_POST
def _update_order_return_status(order):
    """Update order status after a unit return."""
    items = list(order.items.select_related("unit").all())
    if not items:
        return
    statuses = [item.unit.status for item in items]
    all_returned = all(s == ProductUnit.RETURNED for s in statuses)
    any_returned = any(s == ProductUnit.RETURNED for s in statuses)

    if order.status in (ShippingOrder.PAID, ShippingOrder.PARTIAL_PAID):
        order.status = ShippingOrder.PARTIAL_PAID if not all_returned else ShippingOrder.RETURNED
    elif order.status in (ShippingOrder.LIVRE, ShippingOrder.PARTIAL_LIVRE):
        order.status = ShippingOrder.PARTIAL_LIVRE if not all_returned else ShippingOrder.RETURNED
    elif order.status == ShippingOrder.CLOSED:
        if all_returned:
            order.status = ShippingOrder.RETURNED
        elif any_returned:
            order.status = ShippingOrder.PARTIAL_RETURNED
    order.save(update_fields=["status"])


def api_scan_return(request):
    from .barcode_parser import is_bordereau_barcode
    data = json.loads(request.body)
    barcode = data.get("barcode", "").strip().upper()
    if not barcode:
        return JsonResponse({"status": "error", "message": "Barcode vide."}, status=400)

    if is_bordereau_barcode(barcode):
        try:
            order = ShippingOrder.objects.prefetch_related("items__unit__variant__product").get(bordereau_barcode=barcode)
        except ShippingOrder.DoesNotExist:
            return JsonResponse({"status": "error", "message": f"Aucun ordre trouvé pour : {barcode}", "code": "ORDER_NOT_FOUND", "barcode": barcode})
        items = order.items.select_related("unit__variant__product")
        returnable = [i for i in items if i.unit.status in (ProductUnit.SHIPPED, ProductUnit.PAID)]
        if not returnable:
            return JsonResponse({"status": "error", "message": "Aucune unité retournable dans cet ordre."})
        items_data = [{"barcode": i.unit.barcode, "size": i.unit.size, "status": i.unit.status,
                       "product_name": i.unit.variant.product.name, "color_label": i.unit.variant.color_label,
                       "sell_price": str(i.unit.variant.product.sell_price),
                       "image_url": i.unit.variant.image.url if i.unit.variant.image else None}
                      for i in items]
        if len(returnable) == 1:
            unit_data, reconciliation = _do_return_unit(returnable[0].unit)
            return JsonResponse({"status": "ok", "type": "order_single",
                                 "message": f"Unité {unit_data['barcode']} retournée automatiquement.",
                                 "unit": unit_data, "reconciliation": reconciliation})
        return JsonResponse({"status": "ok", "type": "order_multiple",
                             "order_bordereau": order.bordereau_barcode,
                             "order_id": order.id, "items": items_data})

    try:
        unit = ProductUnit.objects.select_related("variant__product").get(barcode=barcode)
    except ProductUnit.DoesNotExist:
        return JsonResponse({"status": "error", "message": f"Unite introuvable : {barcode}"})
    if unit.status not in (ProductUnit.SHIPPED, ProductUnit.PAID):
        msgs = {
            ProductUnit.IN_STOCK: "déjà en stock",
            ProductUnit.RETURNED: "déjà retournée",
        }
        return JsonResponse({"status": "error", "message": f"Impossible — {msgs.get(unit.status, unit.get_status_display())}."})
    unit_data, reconciliation = _do_return_unit(unit)
    return JsonResponse({"status": "ok", "type": "unit_returned",
                         "message": f"{unit_data['product_name']} {unit_data['color_label']} taille {unit_data['size']} retourné.",
                         "unit": unit_data, "reconciliation": reconciliation})


@csrf_exempt
@require_POST
def api_return_multiple(request):
    data = json.loads(request.body)
    barcodes = data.get("barcodes", [])
    if not barcodes:
        return JsonResponse({"status": "error", "message": "Aucune unité sélectionnée."})
    returned_units = []
    reconciliations = []
    for barcode in barcodes:
        try:
            unit = ProductUnit.objects.select_related("variant__product").get(barcode=barcode)
            if unit.status in (ProductUnit.SHIPPED, ProductUnit.PAID):
                unit_data, reconciliation = _do_return_unit(unit)
                returned_units.append(unit_data)
                if reconciliation:
                    reconciliations.append(reconciliation)
        except ProductUnit.DoesNotExist:
            pass
    if not returned_units:
        return JsonResponse({"status": "error", "message": "Aucune unité retournée."})
    combined = {"message": " | ".join(r["message"] for r in reconciliations)} if reconciliations else None
    return JsonResponse({"status": "ok", "message": f"{len(returned_units)} unité(s) retournée(s).",
                         "returned_units": returned_units, "reconciliation": combined})


@csrf_exempt
@require_POST
def api_scan_payment(request):
    """
    Scan a payment barcode from the shipping company slip.
    - If it matches a closed order's payment_barcode → return order details for review
    - If another payment barcode is scanned → mark previous as fully paid
    """
    data = json.loads(request.body)
    barcode = data.get("barcode", "").strip()
    if not barcode:
        return JsonResponse({"status": "error", "message": "Barcode vide."})

    # Check if there's a pending payment order being reviewed
    pending_order_id = data.get("pending_order_id")

    # If another barcode scanned while reviewing an order → mark it paid
    if pending_order_id:
        try:
            prev_order = ShippingOrder.objects.get(pk=pending_order_id)
            if prev_order.status == ShippingOrder.CLOSED:
                prev_order.status = ShippingOrder.PAID
                prev_order.paid_at = timezone.now()
                prev_order.save()
                # Mark all shipped items as sold
                for item in prev_order.items.select_related("unit"):
                    if item.unit.status == ProductUnit.SHIPPED:
                        item.unit.status = ProductUnit.PAID
                        item.unit.save()
                        StockMovement.objects.create(
                            unit=item.unit, movement_type=StockMovement.PAID,
                            reference=prev_order.bordereau_barcode,
                        )
        except ShippingOrder.DoesNotExist:
            pass

    # Find the order for the new barcode
    try:
        order = ShippingOrder.objects.prefetch_related(
            "items__unit__variant__product__variants"
        ).get(bordereau_barcode=barcode)
    except ShippingOrder.DoesNotExist:
        # Try payment_barcode field too
        try:
            order = ShippingOrder.objects.prefetch_related(
                "items__unit__variant__product__variants"
            ).get(payment_barcode=barcode)
        except ShippingOrder.DoesNotExist:
            return JsonResponse({"status": "error", "message": f"Aucun ordre trouvé pour : {barcode}", "code": "NOT_FOUND"})

    if order.status == ShippingOrder.PAID:
        return JsonResponse({"status": "error", "message": f"Cet ordre est déjà marqué comme payé.", "code": "ALREADY_PAID"})

    items_data = []
    for item in order.items.select_related("unit__variant__product"):
        unit = item.unit
        variant = unit.variant
        # Only shipped units can be marked as sold
        # returned/pending_return → locked as refused
        can_be_sold = unit.status == ProductUnit.SHIPPED
        auto_refused = unit.status in (ProductUnit.RETURNED, ProductUnit.RETURNED)
        items_data.append({
            "order_item_id": item.id,
            "barcode": unit.barcode,
            "size": unit.size,
            "product_name": variant.product.name,
            "color_label": variant.color_label,
            "sell_price": str(variant.product.sell_price),
            "status": unit.status,
            "can_be_sold": can_be_sold,
            "auto_refused": auto_refused,
            "image_url": variant.image.url if variant.image else None,
        })

    total = sum(Decimal(i["sell_price"]) for i in items_data)

    return JsonResponse({
        "status": "ok",
        "type": "payment_review",
        "order": {
            "id": order.id,
            "bordereau_barcode": order.bordereau_barcode,
            "status": order.status,
            "opened_at": order.opened_at.strftime("%d/%m/%Y %H:%M"),
            "unit_count": order.unit_count,
            "expected_amount": str(total + 7),
            "shipping_fee": "7.000",
        },
        "items": items_data,
    })


@csrf_exempt
@require_POST
def api_confirm_payment(request):
    """Confirm payment with optional modifications (refused items, adjusted price)."""
    data = json.loads(request.body)
    order_id = data.get("order_id")
    sold_barcodes = data.get("sold_barcodes", [])   # items client accepted
    refused_barcodes = data.get("refused_barcodes", [])  # items client refused
    amount_collected = Decimal(str(data.get("amount_collected", "0")))
    notes = data.get("notes", "")

    try:
        order = ShippingOrder.objects.get(pk=order_id)
    except ShippingOrder.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Ordre introuvable."})

    # Mark sold items — skip any returned units (they cannot be paid directly)
    for barcode in sold_barcodes:
        try:
            unit = ProductUnit.objects.get(barcode=barcode)
            if unit.status == ProductUnit.RETURNED:
                continue  # Cannot pay a returned unit
            unit.status = ProductUnit.PAID
            unit.save()
            StockMovement.objects.create(unit=unit, movement_type=StockMovement.PAID, reference=order.bordereau_barcode)
            OrderItem.objects.filter(order=order, unit=unit).update(status_at_payment=ProductUnit.PAID)
        except ProductUnit.DoesNotExist:
            pass

    # Mark refused items as pending_return
    for barcode in refused_barcodes:
        try:
            unit = ProductUnit.objects.get(barcode=barcode)
            unit.status = ProductUnit.RETURNED
            unit.save()
            StockMovement.objects.create(unit=unit, movement_type=StockMovement.RETURNED, reference=order.bordereau_barcode)
            # Save snapshot on OrderItem
            OrderItem.objects.filter(order=order, unit=unit).update(status_at_payment=ProductUnit.RETURNED)
        except ProductUnit.DoesNotExist:
            pass

    # Calculate expected
    sold_total = sum(
        ProductUnit.objects.get(barcode=b).variant.product.sell_price
        for b in sold_barcodes
        if ProductUnit.objects.filter(barcode=b).exists()
    )
    expected = sold_total + 7

    # Create payment record
    Payment.objects.update_or_create(
        order=order,
        defaults={
            "amount_expected": expected,
            "amount_collected": amount_collected,
            "shipping_fee": Decimal("7"),
            "notes": notes,
        }
    )

    # Mark order as paid — also fix any remaining shipped units
    order.status = ShippingOrder.PAID
    order.paid_at = timezone.now()
    order.amount_collected = amount_collected
    order.notes = notes
    order.save()

    # Safety: mark any remaining shipped units as paid
    for item in order.items.select_related("unit"):
        if item.unit.status == ProductUnit.SHIPPED:
            item.unit.status = ProductUnit.PAID
            item.unit.save()
            OrderItem.objects.filter(pk=item.pk).update(status_at_payment=ProductUnit.PAID)

    pending_count = len(refused_barcodes)
    return JsonResponse({
        "status": "ok",
        "message": f"Paiement confirmé. {len(sold_barcodes)} vendu(s), {pending_count} en attente de retour physique.",
        "pending_returns": pending_count,
        "amount_collected": str(amount_collected),
        "amount_expected": str(expected),
    })


@csrf_exempt
@require_POST
def api_close_order(request):
    """Manually close the currently open order with an expected amount."""
    data = json.loads(request.body)
    expected_amount = Decimal(str(data.get("expected_amount", "0")))

    open_order = ShippingOrder.objects.filter(status=ShippingOrder.OPEN).first()
    if not open_order:
        return JsonResponse({"status": "error", "message": "Aucun ordre ouvert."})

    if open_order.items.count() == 0:
        return JsonResponse({
            "status": "error",
            "message": f"Impossible de fermer l'ordre {open_order.bordereau_barcode} — aucune unité scannée ! Scannez au moins un produit avant de fermer."
        })

    open_order.status = ShippingOrder.CLOSED
    open_order.closed_at = timezone.now()
    open_order.amount_collected = expected_amount
    open_order.save()

    for item in open_order.items.select_related("unit"):
        item.unit.status = ProductUnit.SHIPPED
        item.unit.save()
        item.status_at_close = ProductUnit.SHIPPED
        item.save(update_fields=["status_at_close"])
        StockMovement.objects.create(
            unit=item.unit,
            movement_type=StockMovement.SHIPPED,
            reference=open_order.bordereau_barcode,
        )

    return JsonResponse({
        "status": "ok",
        "message": f"Ordre {open_order.bordereau_barcode} fermé.",
        "order_id": open_order.id,
        "unit_count": open_order.unit_count,
        "expected_amount": str(expected_amount),
    })


@csrf_exempt
@require_POST
def api_update_order_amount(request, pk):
    """Update the amount collected for an order from order detail page."""
    data = json.loads(request.body)
    amount = Decimal(str(data.get("amount_collected", "0")))
    try:
        order = ShippingOrder.objects.get(pk=pk)
    except ShippingOrder.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Ordre introuvable."})

    old_amount = order.amount_collected or Decimal("0")
    order.amount_collected = amount
    order.save()

    # Also update payment record if exists
    note = None
    try:
        payment = order.payment
        payment.amount_collected = amount
        payment.save()
    except Exception:
        pass

    diff = amount - old_amount
    if diff != 0:
        note = f"Montant {'augmenté' if diff > 0 else 'réduit'} de {abs(diff):.3f} TND (ancien: {old_amount:.3f} TND)"

    return JsonResponse({"status": "ok", "note": note})


@login_required(login_url="/login/")
def search(request):
    return render(request, "inventory/search.html", {})


def api_search(request):
    q = request.GET.get("q", "").strip()
    if not q:
        return JsonResponse({"units": [], "orders": []})

    units_data = []
    orders_data = []

    # Search units by barcode or product name
    from django.db.models import Q
    units = ProductUnit.objects.select_related(
        "variant__product", "variant"
    ).filter(
        Q(barcode__icontains=q) |
        Q(variant__product__name__icontains=q) |
        Q(variant__color_name__icontains=q) |
        Q(variant__color_label__icontains=q)
    )[:20]

    for unit in units:
        variant = unit.variant
        last_order_item = unit.order_items.select_related("order").order_by("-scanned_at").first()
        units_data.append({
            "barcode": unit.barcode,
            "size": unit.size,
            "status": unit.status,
            "status_label": unit.get_status_display(),
            "product_name": variant.product.name,
            "color_label": variant.color_label,
            "sell_price": str(variant.product.sell_price),
            "image_url": variant.image.url if variant.image else None,
            "created_at": unit.created_at.strftime("%d/%m/%Y"),
            "order_bordereau": last_order_item.order.bordereau_barcode if last_order_item else None,
            "order_id": last_order_item.order.id if last_order_item else None,
        })

    # Search orders by bordereau barcode
    orders = ShippingOrder.objects.prefetch_related(
        "items__unit__variant__product"
    ).filter(bordereau_barcode__icontains=q)[:10]

    for order in orders:
        items_data = []
        for item in order.items.select_related("unit__variant__product")[:6]:
            items_data.append({
                "product_name": item.unit.variant.product.name,
                "color_label": item.unit.variant.color_label,
                "size": item.unit.size,
                "image_url": item.unit.variant.image.url if item.unit.variant.image else None,
            })
        orders_data.append({
            "id": order.id,
            "bordereau_barcode": order.bordereau_barcode,
            "status": order.status,
            "opened_at": order.opened_at.strftime("%d/%m/%Y %H:%M"),
            "unit_count": order.unit_count,
            "items": items_data,
        })

    return JsonResponse({"units": units_data, "orders": orders_data})


@csrf_exempt
@require_POST
def api_fix_order_units(request, pk):
    """Resync unit statuses with their order status."""
    try:
        order = ShippingOrder.objects.prefetch_related("items__unit").get(pk=pk)
    except ShippingOrder.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Ordre introuvable."})

    fixed = 0
    for item in order.items.select_related("unit"):
        unit = item.unit
        if order.status == ShippingOrder.PAID and unit.status == ProductUnit.SHIPPED:
            unit.status = ProductUnit.PAID
            unit.save()
            OrderItem.objects.filter(pk=item.pk).update(status_at_payment=ProductUnit.PAID)
            fixed += 1
        elif order.status == ShippingOrder.CLOSED and unit.status not in (ProductUnit.RETURNED, ProductUnit.PAID):
            unit.status = ProductUnit.SHIPPED
            unit.save()
            fixed += 1

    return JsonResponse({"status": "ok", "message": f"{fixed} unité(s) resynchronisée(s).", "fixed": fixed})


@csrf_exempt
@require_POST
def api_delete_order(request, pk):
    """Delete an order and return all units to in_stock."""
    data = json.loads(request.body)
    confirmation = data.get("confirmation", "")
    if confirmation != "DELETE":
        return JsonResponse({"status": "error", "message": "Confirmation incorrecte."})
    try:
        order = ShippingOrder.objects.get(pk=pk)
    except ShippingOrder.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Ordre introuvable."})
    if order.status == ShippingOrder.PAID:
        return JsonResponse({"status": "error", "message": "Impossible de supprimer un ordre déjà payé."})
    # Return all units to in_stock
    for item in order.items.select_related("unit"):
        item.unit.status = ProductUnit.IN_STOCK
        item.unit.save()
        StockMovement.objects.create(
            unit=item.unit, movement_type=StockMovement.RECEIVED,
            reference=f"SUPPRESSION ORDRE {order.bordereau_barcode}"
        )
    order.delete()
    return JsonResponse({"status": "ok", "message": "Ordre supprimé, unités remises en stock."})


@csrf_exempt
@require_POST
def api_order_remove_unit(request, pk):
    """Remove a unit from an order and return it to in_stock."""
    data = json.loads(request.body)
    confirmation = data.get("confirmation", "")
    barcode = data.get("barcode", "")
    if confirmation != "MODIFY":
        return JsonResponse({"status": "error", "message": "Confirmation incorrecte."})
    try:
        order = ShippingOrder.objects.get(pk=pk)
        unit = ProductUnit.objects.get(barcode=barcode)
        item = OrderItem.objects.get(order=order, unit=unit)
    except (ShippingOrder.DoesNotExist, ProductUnit.DoesNotExist, OrderItem.DoesNotExist):
        return JsonResponse({"status": "error", "message": "Unité ou ordre introuvable."})
    if order.status == ShippingOrder.PAID:
        return JsonResponse({"status": "error", "message": "Impossible de modifier un ordre payé."})
    item.delete()
    unit.status = ProductUnit.IN_STOCK
    unit.save()
    StockMovement.objects.create(
        unit=unit, movement_type=StockMovement.RECEIVED,
        reference=f"MODIFICATION ORDRE {order.bordereau_barcode}"
    )
    return JsonResponse({"status": "ok", "message": f"{barcode} retiré et remis en stock."})


@csrf_exempt
@require_POST
def api_order_add_unit(request, pk):
    """Add a unit to an existing order by scanning barcode."""
    data = json.loads(request.body)
    confirmation = data.get("confirmation", "")
    barcode = data.get("barcode", "").strip().upper()
    if confirmation != "MODIFY":
        return JsonResponse({"status": "error", "message": "Confirmation incorrecte."})
    try:
        order = ShippingOrder.objects.get(pk=pk)
    except ShippingOrder.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Ordre introuvable."})
    if order.status == ShippingOrder.PAID:
        return JsonResponse({"status": "error", "message": "Impossible de modifier un ordre payé."})
    try:
        unit = ProductUnit.objects.select_related("variant__product").get(barcode=barcode)
    except ProductUnit.DoesNotExist:
        return JsonResponse({"status": "error", "message": f"Unité introuvable : {barcode}"})
    if unit.status not in (ProductUnit.IN_STOCK, ProductUnit.RETURNED):
        return JsonResponse({"status": "error", "message": f"{barcode} n'est pas en stock — statut : {unit.get_status_display()}"})
    if OrderItem.objects.filter(order=order, unit=unit).exists():
        return JsonResponse({"status": "error", "message": f"{barcode} est déjà dans cet ordre."})
    OrderItem.objects.create(order=order, unit=unit, status_at_scan=ProductUnit.SHIPPED)
    unit.status = ProductUnit.SHIPPED
    unit.save()
    variant = unit.variant
    return JsonResponse({
        "status": "ok",
        "message": f"{variant.product.name} {variant.color_label} taille {unit.size} ajouté.",
        "unit": {
            "barcode": unit.barcode, "size": unit.size,
            "product_name": variant.product.name, "color_label": variant.color_label,
            "sell_price": str(variant.product.sell_price),
            "image_url": variant.image.url if variant.image else None,
        }
    })


# ---------------------------------------------------------------------------
# DASHBOARD & DETAIL VIEWS
# ---------------------------------------------------------------------------

@login_required(login_url="/login/")
def dashboard(request):
    products = Product.objects.prefetch_related("variants__units").all()
    open_order = ShippingOrder.objects.filter(status=ShippingOrder.OPEN).first()
    recent_orders = ShippingOrder.objects.order_by("-opened_at")[:10]
    now = timezone.now()
    orders_this_month = ShippingOrder.objects.filter(opened_at__year=now.year, opened_at__month=now.month).count()
    total_in_stock = ProductUnit.objects.filter(status=ProductUnit.IN_STOCK).count()
    total_shipped = ProductUnit.objects.filter(status=ProductUnit.SHIPPED).count()
    low_stock = [p for p in products if p.total_stock <= p.alert_threshold]

    # Size alerts
    from .models import SizeAlert
    size_alerts = [sa for sa in SizeAlert.objects.select_related("variant__product").all() if sa.is_triggered]
    total_products = products.count()

    # Alerts: shipped units that belong to paid orders = waiting to come back
    waiting_return = ProductUnit.objects.filter(
        status=ProductUnit.SHIPPED,
        order_items__order__status=ShippingOrder.PAID,
    ).distinct().count()
    overdue_orders = [o for o in ShippingOrder.objects.filter(status=ShippingOrder.CLOSED) if o.is_overdue]

    return render(request, "inventory/dashboard.html", {
        "products": products, "open_order": open_order, "recent_orders": recent_orders,
        "low_stock": low_stock, "total_in_stock": total_in_stock,
        "total_shipped": total_shipped, "orders_this_month": orders_this_month,
        "pending_returns": waiting_return, "total_products": total_products, "size_alerts": size_alerts, "overdue_orders": overdue_orders,
    })


@login_required(login_url="/login/")
def order_detail(request, pk):
    from decimal import Decimal
    order = get_object_or_404(ShippingOrder, pk=pk)
    items = order.items.select_related("unit__variant__product", "unit__variant").order_by("-scanned_at")
    # Auto-calculate total: only non-returned items + 7 TND shipping
    ACTIVE = ("paid", "shipped", "shipped")
    calculated_total = sum(
        item.unit.variant.product.sell_price
        for item in items
        if item.display_status in ACTIVE
    ) + Decimal("7")
    return render(request, "inventory/order_detail.html", {
        "order": order,
        "items": items,
        "calculated_total": calculated_total,
    })


@login_required(login_url="/login/")
def revenue(request):
    if not request.user.is_staff:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("Accès réservé aux administrateurs.")

    from datetime import datetime, date
    RETURN_FEE = Decimal("1")

    date_from_str = request.GET.get("date_from", "")
    date_to_str   = request.GET.get("date_to", "")

    # Parse dates
    date_from = None
    date_to   = None
    try:
        if date_from_str:
            date_from = datetime.strptime(date_from_str, "%Y-%m-%d").date()
        if date_to_str:
            date_to = datetime.strptime(date_to_str, "%Y-%m-%d").date()
    except ValueError:
        pass

    # Filter paid orders by paid_at date
    paid_orders_qs = ShippingOrder.objects.filter(status=ShippingOrder.PAID)
    if date_from:
        paid_orders_qs = paid_orders_qs.filter(paid_at__date__gte=date_from)
    if date_to:
        paid_orders_qs = paid_orders_qs.filter(paid_at__date__lte=date_to)
    paid_orders_qs = paid_orders_qs.order_by("-paid_at")

    # Calculate from filtered orders
    total_sell       = Decimal("0")
    total_buy        = Decimal("0")
    total_paid_units = 0
    total_returns    = 0
    order_rows       = []

    for order in paid_orders_qs:
        items = order.items.select_related("unit__variant__product")
        paid_items   = [i for i in items if i.display_status == ProductUnit.PAID]
        return_items = [i for i in items if i.display_status == ProductUnit.RETURNED]
        sell_total = sum(i.unit.variant.product.sell_price for i in paid_items)
        buy_total  = sum(i.unit.variant.product.buy_price  for i in paid_items)
        ret_fees   = RETURN_FEE * len(return_items)
        total_sell       += sell_total
        total_buy        += buy_total
        total_paid_units += len(paid_items)
        total_returns    += len(return_items)
        order_rows.append({
            "order": order,
            "paid_count": len(paid_items),
            "return_count": len(return_items),
            "sell_total": sell_total,
            "buy_total": buy_total,
            "net": sell_total - buy_total - ret_fees,
        })

    gross_margin = total_sell - total_buy
    margin_pct   = (gross_margin / total_sell * 100) if total_sell > 0 else Decimal("0")
    return_fees  = RETURN_FEE * total_returns
    net_revenue  = gross_margin - return_fees

    return render(request, "inventory/revenue.html", {
        "total_sell": total_sell, "total_buy": total_buy,
        "total_paid_units": total_paid_units, "gross_margin": gross_margin,
        "margin_pct": margin_pct, "total_returns": total_returns,
        "return_fees": return_fees, "net_revenue": net_revenue,
        "order_rows": order_rows,
        "date_from": date_from_str, "date_to": date_to_str,
    })


@login_required(login_url="/login/")
def navex_sync_page(request):
    if not request.user.is_staff:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("Accès réservé.")
    return render(request, "inventory/navex_sync.html", {})


@csrf_exempt
@require_POST
def api_navex_sync(request):
    """Run Navex sync — check all shipped orders."""
    if not request.user.is_staff:
        return JsonResponse({"status": "error", "message": "Accès refusé."}, status=403)
    from .navex_sync import sync_shipped_orders
    results = sync_shipped_orders()
    return JsonResponse(results)


def admin_panel(request):
    if not request.user.is_staff:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("Accès réservé aux administrateurs.")
    from django.contrib.auth.models import User
    from .models import Payment
    return render(request, "inventory/admin_panel.html", {
        "product_count": Product.objects.count(),
        "variant_count": ProductVariant.objects.count(),
        "unit_count": ProductUnit.objects.count(),
        "order_count": ShippingOrder.objects.count(),
        "payment_count": Payment.objects.count(),
        "user_count": User.objects.count(),
    })


@login_required(login_url="/login/")
@csrf_exempt
@require_POST
def api_navex_status(request, pk):
    """Check Navex status for an order — read only, never modifies our data."""
    import urllib.request
    import urllib.parse

    order = get_object_or_404(ShippingOrder, pk=pk)

    try:
        data = urllib.parse.urlencode({'code': order.bordereau_barcode, 'include_prix': '1'}).encode()
        req = urllib.request.Request(
            'https://app.navex.tn/api/rashop-etat-UI3UBFX5QQRYSP3JHOG1ZJH2W8K1FT18/v1/post.php',
            data=data,
            method='POST'
        )
        req.add_header('Content-Type', 'application/x-www-form-urlencoded')
        with urllib.request.urlopen(req, timeout=10) as response:
            import json as json_lib
            result = json_lib.loads(response.read().decode())
            return JsonResponse({"status": "ok", "navex": result})
    except Exception as e:
        return JsonResponse({"status": "error", "message": f"Erreur Navex : {str(e)}"})


@csrf_exempt
@require_POST
def api_confirm_payment_from_navex(request, pk):
    """Confirm payment for an order using Navex price."""
    import urllib.request, urllib.parse
    data = json.loads(request.body)
    amount = Decimal(str(data.get("amount", "0")))

    try:
        order = ShippingOrder.objects.get(pk=pk)
    except ShippingOrder.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Ordre introuvable."})

    if order.status == ShippingOrder.PAID:
        return JsonResponse({"status": "error", "message": "Ordre deja paye."})

    with transaction.atomic():
        order.status = ShippingOrder.PAID
        order.paid_at = timezone.now()
        order.amount_collected = amount
        order.save()
        for item in order.items.select_related("unit"):
            if item.unit.status == ProductUnit.SHIPPED:
                item.unit.status = ProductUnit.PAID
                item.unit.save()
                OrderItem.objects.filter(pk=item.pk).update(status_at_payment=ProductUnit.PAID)
                StockMovement.objects.create(
                    unit=item.unit, movement_type=StockMovement.PAID,
                    reference=order.bordereau_barcode,
                )

    return JsonResponse({"status": "ok", "message": f"Ordre {order.bordereau_barcode} marque comme paye — {amount} TND."})


@csrf_exempt
def api_navex_en_attente(request):
    """Get all en attente orders from Navex and try to match with our products."""
    import urllib.request, urllib.parse

    try:
        data = urllib.parse.urlencode({"getattente": "1"}).encode()
        req = urllib.request.Request(
            "https://app.navex.tn/api/rashop-etat-UI3UBFX5QQRYSP3JHOG1ZJH2W8K1FT18/v1/post.php",
            data=data, method="POST"
        )
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=15) as resp:
            import json as json_lib
            navex_data = json_lib.loads(resp.read().decode())

        colis_list = navex_data.get("colis", [])

        # Only skip orders that are already CLOSED or PAID — open orders still count as pending
        our_barcodes = set(ShippingOrder.objects.filter(
            status__in=(ShippingOrder.CLOSED, ShippingOrder.PAID)
        ).values_list("bordereau_barcode", flat=True))

        result = []
        for colis in colis_list:
            code_barre = colis.get("code_barre", "")
            designation = colis.get("designation", "")
            prix = colis.get("prix", "")

            # Skip if already scanned in our system
            if code_barre in our_barcodes:
                continue

            # Use scan_service helper for consistent matching
            from .scan_service import _get_matched_products
            matched_products = _get_matched_products(designation)

            result.append({
                "code_barre": code_barre,
                "designation": designation,
                "prix": prix,
                "nom": colis.get("nom", "") or colis.get("client_nom", "") or colis.get("name", ""),
                "tel": colis.get("tel", "") or colis.get("phone", "") or colis.get("telephone", ""),
                "ville": colis.get("ville", "") or colis.get("city", ""),
                "matched_products": matched_products,
                "recognized": len(matched_products) > 0,
            })

        # Populate scan_service cache for instant prediction on scan
        from . import scan_service
        scan_service.navexMap_cache = {c["code_barre"]: c for c in result}

        return JsonResponse({
            "status": "ok",
            "total": len(result),
            "colis": result,
        })

    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)})


@login_required(login_url="/login/")
def a_verifier(request):
    if not request.user.is_staff:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden()

    now = timezone.now()
    closed_orders = ShippingOrder.objects.filter(
        status__in=(ShippingOrder.CLOSED, ShippingOrder.PARTIAL_RETURNED)
    ).order_by("closed_at")

    orders_to_verify = []
    for order in closed_orders:
        if not order.closed_at:
            continue
        closed_weekday = order.closed_at.weekday()
        limit_hours = 58 if closed_weekday == 5 else 34
        hours_since = (now - order.closed_at).total_seconds() / 3600
        if hours_since >= limit_hours:
            verification, _ = OrderVerification.objects.get_or_create(order=order)
            # Reset treated if it was treated on a previous day
            if verification.treated and verification.treated_at:
                if verification.treated_at.date() < now.date():
                    verification.treated = False
                    verification.treated_at = None
                    verification.save(update_fields=["treated", "treated_at"])
            orders_to_verify.append({
                "order": order,
                "verification": verification,
                "hours_since": round(hours_since, 1),
                "limit_hours": limit_hours,
            })

    # Fetch Navex status for all orders at once
    navex_map = {}
    try:
        import urllib.request, urllib.parse
        codes = [o["order"].bordereau_barcode for o in orders_to_verify]
        if codes:
            codes_string = ", ".join(codes)
            data = urllib.parse.urlencode({"codes": codes_string, "include_prix": "1"}).encode()
            req = urllib.request.Request(
                "https://app.navex.tn/api/rashop-etat-UI3UBFX5QQRYSP3JHOG1ZJH2W8K1FT18/v1/post.php",
                data=data, method="POST"
            )
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            with urllib.request.urlopen(req, timeout=15) as resp:
                import json as json_lib
                navex_data = json_lib.loads(resp.read().decode())
            for result in navex_data.get("results", []):
                if result.get("status") == 1:
                    navex_map[result["code"]] = result.get("etat", "")
    except Exception:
        pass

    # Filter out orders that Navex says are delivered
    DELIVERED_STATES = ("Livrer", "Livrer Paye", "Livré", "Livré Payé", "Livree", "Retourné", "Retour recu", "Rtn client/agence")
    filtered_orders = []
    for o in orders_to_verify:
        etat = navex_map.get(o["order"].bordereau_barcode, "Inconnu")
        o["navex_etat"] = etat
        if etat not in DELIVERED_STATES:
            filtered_orders.append(o)
    orders_to_verify = filtered_orders

    treated_count = sum(1 for o in orders_to_verify if o["verification"].treated)
    untreated_count = len(orders_to_verify) - treated_count

    return render(request, "inventory/a_verifier.html", {
        "orders": orders_to_verify,
        "treated_count": treated_count,
        "untreated_count": untreated_count,
    })


@csrf_exempt
@require_POST
def api_mark_treated(request, pk):
    try:
        order = ShippingOrder.objects.get(pk=pk)
        verification, _ = OrderVerification.objects.get_or_create(order=order)
        verification.treated = not verification.treated
        verification.treated_at = timezone.now() if verification.treated else None
        verification.save()
        return JsonResponse({"status": "ok", "treated": verification.treated})
    except ShippingOrder.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Ordre introuvable."})


@csrf_exempt
@require_POST
def api_save_navex_info(request, pk):
    """Save Navex info to an order — called from JS after scan."""
    try:
        data = json.loads(request.body)
        ShippingOrder.objects.filter(pk=pk).update(
            amount_collected=data.get("prix") or None,
            client_name=data.get("nom", ""),
            client_phone=data.get("tel", ""),
            client_address=data.get("adresse", ""),
            client_ville=data.get("ville", ""),
            navex_designation=data.get("designation", ""),
        )
        return JsonResponse({"status": "ok"})
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)})


def api_get_order_amount(request, pk):
    """Get amount for an order — from DB or fetch from Navex."""
    try:
        order = ShippingOrder.objects.get(pk=pk)
        # If we have it saved, return it
        if order.amount_collected:
            return JsonResponse({
                "status": "ok",
                "amount_collected": str(order.amount_collected),
            })
        # Otherwise fetch from Navex single status API
        try:
            import urllib.request, urllib.parse
            data = urllib.parse.urlencode({
                "code": order.bordereau_barcode,
                "include_prix": "1"
            }).encode()
            req = urllib.request.Request(
                "https://app.navex.tn/api/rashop-etat-UI3UBFX5QQRYSP3JHOG1ZJH2W8K1FT18/v1/post.php",
                data=data, method="POST"
            )
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            with urllib.request.urlopen(req, timeout=5) as resp:
                import json as json_lib
                navex_data = json_lib.loads(resp.read().decode())
            prix = navex_data.get("prix")
            if prix:
                # Save it
                ShippingOrder.objects.filter(pk=pk).update(amount_collected=prix)
                return JsonResponse({"status": "ok", "amount_collected": str(prix)})
        except Exception:
            pass
        return JsonResponse({"status": "ok", "amount_collected": None})
    except ShippingOrder.DoesNotExist:
        return JsonResponse({"status": "error"})


@csrf_exempt
@require_POST  
def api_create_return_order(request):
    """Create a new return order from an unknown barcode scanned in retour section."""
    data = json.loads(request.body)
    barcode = data.get("barcode", "").strip().upper()
    if not barcode:
        return JsonResponse({"status": "error", "message": "Barcode manquant."})
    
    # Check it doesn't already exist
    if ShippingOrder.objects.filter(bordereau_barcode=barcode).exists():
        return JsonResponse({"status": "error", "message": "Ce bordereau existe déjà."})
    
    order = ShippingOrder.objects.create(
        bordereau_barcode=barcode,
        status=ShippingOrder.OPEN,
        notes="Ordre retour",
    )
    return JsonResponse({
        "status": "ok",
        "order": {"id": order.id, "bordereau_barcode": order.bordereau_barcode},
        "message": f"Ordre retour {barcode} créé.",
    })


@csrf_exempt
@require_POST
def api_return_unit_to_order(request, pk):
    """Move a unit from its original order to a return order."""
    data = json.loads(request.body)
    barcode = data.get("barcode", "").strip().upper()
    
    try:
        return_order = ShippingOrder.objects.get(pk=pk)
    except ShippingOrder.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Ordre retour introuvable."})
    
    try:
        unit = ProductUnit.objects.select_related("variant__product").get(barcode=barcode)
    except ProductUnit.DoesNotExist:
        return JsonResponse({"status": "error", "message": f"Unité {barcode} introuvable."})
    
    if unit.status not in (ProductUnit.SHIPPED, ProductUnit.PAID):
        return JsonResponse({"status": "error", "message": f"Cette unité ne peut pas être retournée (statut: {unit.status})."})
    
    # Find original order
    original_item = unit.order_items.select_related("order").order_by("-scanned_at").first()
    original_order = original_item.order if original_item else None
    
    with transaction.atomic():
        # Create item in return order
        OrderItem.objects.get_or_create(
            order=return_order,
            unit=unit,
            defaults={"status_at_scan": unit.status}
        )
        
        # Mark unit as returned
        unit.status = ProductUnit.RETURNED
        unit.save()
        StockMovement.objects.create(
            unit=unit, movement_type=StockMovement.RETURNED,
            reference=return_order.bordereau_barcode
        )
        
        # Update original order status and amount
        if original_order:
            if original_item:
                original_item.status_at_payment = ProductUnit.RETURNED
                original_item.save(update_fields=["status_at_payment"])
            
            # Deduct from original order amount if it was paid at full price
            if original_order.amount_collected:
                unit_price = unit.variant.product.sell_price
                new_amount = max(original_order.amount_collected - unit_price, Decimal("0"))
                original_order.amount_collected = new_amount
            
            _update_order_return_status(original_order)
        
        # Update return order status
        return_items = return_order.items.select_related("unit").all()
        return_statuses = [i.unit.status for i in return_items]
        if return_statuses and all(s == ProductUnit.RETURNED for s in return_statuses):
            return_order.status = ShippingOrder.RETURNED
        else:
            return_order.status = ShippingOrder.PARTIAL_RETURNED
        return_order.save(update_fields=["status"])
    
    return JsonResponse({
        "status": "ok",
        "message": f"{unit.variant.product.name} {unit.variant.color_label} — {unit.size} retourné.",
        "unit": {
            "barcode": unit.barcode,
            "product_name": unit.variant.product.name,
            "color_label": unit.variant.color_label,
            "size": unit.size,
            "image_url": unit.variant.image.url if unit.variant.image else None,
        },
        "original_order": original_order.bordereau_barcode if original_order else None,
    })


@csrf_exempt
@require_POST
def api_set_size_alert(request, variant_pk, size):
    """Set alert threshold for a variant+size combination."""
    if not request.user.is_superuser:
        return JsonResponse({"status": "error", "message": "Admin uniquement."})
    data = json.loads(request.body)
    threshold = int(data.get("threshold", 3))
    alert, created = SizeAlert.objects.get_or_create(
        variant_id=variant_pk, size=size,
        defaults={"threshold": threshold}
    )
    if not created:
        alert.threshold = threshold
        alert.save()
    return JsonResponse({"status": "ok", "threshold": alert.threshold})

@csrf_exempt
def api_get_size_alert(request, variant_pk, size):
    """Get current alert threshold for a variant+size."""
    try:
        alert = SizeAlert.objects.get(variant_id=variant_pk, size=size)
        return JsonResponse({"status": "ok", "threshold": alert.threshold})
    except SizeAlert.DoesNotExist:
        return JsonResponse({"status": "ok", "threshold": None})


@csrf_exempt
@require_POST
def api_log_scan_session(request):
    """Save a closed order to the daily scan session log."""
    data = json.loads(request.body)
    today = timezone.now().date()
    ScanSessionLog.objects.create(
        bordereau_barcode=data.get("bordereau", ""),
        designation=data.get("designation", ""),
        unit_count=data.get("unit_count", 0),
        is_correct=data.get("is_correct", True),
        reason=data.get("reason", ""),
        session_date=today,
    )
    return JsonResponse({"status": "ok"})


@login_required(login_url="/login/")
def api_get_scan_session(request):
    """Get today's scan session log."""
    today = timezone.now().date()
    logs = ScanSessionLog.objects.filter(session_date=today).order_by("-scanned_at")
    return JsonResponse({
        "status": "ok",
        "correct": [{"bc": l.bordereau_barcode, "designation": l.designation, "units": l.unit_count, "time": l.scanned_at.strftime("%H:%M")} for l in logs if l.is_correct],
        "wrong": [{"bc": l.bordereau_barcode, "designation": l.designation, "units": l.unit_count, "reason": l.reason, "time": l.scanned_at.strftime("%H:%M")} for l in logs if not l.is_correct],
    })


def navex_sync(request):
    """Sync page — shows all shipped orders with their Navex status."""
    if not request.user.is_staff:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("Accès réservé.")
    shipped_orders = ShippingOrder.objects.filter(
        status=ShippingOrder.CLOSED
    ).order_by("-opened_at")
    return render(request, "inventory/navex_sync.html", {
        "shipped_orders": shipped_orders,
    })


@csrf_exempt
def api_navex_sync(request):
    """Call Navex API for multiple barcodes at once."""
    import urllib.request
    import urllib.parse

    # Get all shipped/closed order barcodes
    # Only check CLOSED orders — not yet paid in our system
    orders = ShippingOrder.objects.filter(
        status=ShippingOrder.CLOSED
    ).values("id", "bordereau_barcode", "status", "amount_collected", "opened_at")

    if not orders:
        return JsonResponse({"status": "ok", "results": []})

    codes = [o["bordereau_barcode"] for o in orders]
    codes_string = ", ".join(codes)

    try:
        data = urllib.parse.urlencode({"codes": codes_string, "include_prix": "1"}).encode()
        req = urllib.request.Request(
            "https://app.navex.tn/api/rashop-etat-UI3UBFX5QQRYSP3JHOG1ZJH2W8K1FT18/v1/post.php",
            data=data,
            method="POST"
        )
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=15) as response:
            import json as json_lib
            navex_data = json_lib.loads(response.read().decode())

        # Build a map of code → navex result
        navex_map = {}
        for result in navex_data.get("results", []):
            navex_map[result["code"]] = result

        # Merge our orders with navex data
        merged = []
        for order in orders:
            bc = order["bordereau_barcode"]
            navex = navex_map.get(bc, None)
            navex_etat = navex.get("etat", "Introuvable") if navex and navex.get("status") == 1 else "Introuvable"
            needs_attention = navex_etat in ("Livrer Paye", "Livré", "Livrée", "Livré Payé")
            is_anomaly = navex_etat in ("Retourné", "Retourne", "Annulé", "Annule")

            # Check if order is closed for more than 24h and not yet delivered
            if not needs_attention and not is_anomaly:
                try:
                    order_obj = ShippingOrder.objects.get(pk=order["id"])
                    if order_obj.closed_at:
                        hours_since_close = (timezone.now() - order_obj.closed_at).total_seconds() / 3600
                        if hours_since_close > 24:
                            is_anomaly = True
                except Exception:
                    pass
            # Get price from Navex (include_prix=1) - field is "prix"
            navex_prix = navex.get("prix") if navex else None
            # Calculate our expected total and unit count
            try:
                order_obj = ShippingOrder.objects.get(pk=order["id"])
                order_items = list(order_obj.items.select_related("unit__variant__product"))
                our_total = sum(
                    item.unit.variant.product.sell_price
                    for item in order_items
                ) + Decimal("7")
                unit_count = len(order_items)
            except Exception:
                our_total = None
                unit_count = 0

            price_match = None
            if navex_prix and our_total:
                try:
                    price_match = abs(Decimal(str(navex_prix)) - our_total) < Decimal("0.1")
                except Exception:
                    price_match = None

            # Calculate hours since close for display
            hours_late = None
            try:
                order_obj2 = ShippingOrder.objects.get(pk=order["id"])
                if order_obj2.closed_at:
                    hours_late = round((timezone.now() - order_obj2.closed_at).total_seconds() / 3600, 1)
            except Exception:
                pass

            merged.append({
                "id": order["id"],
                "bordereau_barcode": bc,
                "our_status": order["status"],
                "amount_collected": str(order["amount_collected"] or ""),
                "navex_etat": navex_etat,
                "navex_motif": navex.get("motif", "") if navex else "",
                "navex_livreur": navex.get("livreur", "") if navex else "",
                "navex_prix": str(navex_prix) if navex_prix else None,
                "our_total": str(our_total) if our_total else None,
                "unit_count": unit_count,
                "price_match": price_match,
                "needs_attention": needs_attention,
                "is_anomaly": is_anomaly,
                "hours_late": hours_late,
                "opened_at": order["opened_at"].strftime("%d/%m/%Y %H:%M") if order["opened_at"] else "",
            })

        # Sort: needs attention first
        merged.sort(key=lambda x: (not x["needs_attention"], x["opened_at"]))

        return JsonResponse({"status": "ok", "results": merged, "total": len(merged)})

    except Exception as e:
        return JsonResponse({"status": "error", "message": f"Erreur Navex : {str(e)}"})


def products_list(request):
    products = Product.objects.prefetch_related("variants__units").all()
    total_available = ProductUnit.objects.filter(status__in=(ProductUnit.IN_STOCK, ProductUnit.RETURNED)).count()
    total_shipped = ProductUnit.objects.filter(status=ProductUnit.SHIPPED).count()
    total_paid = ProductUnit.objects.filter(status=ProductUnit.PAID).count()

    # Get all size alerts
    size_alerts = {}
    for alert in SizeAlert.objects.select_related("variant").all():
        key = (alert.variant_id, alert.size)
        size_alerts[key] = alert.threshold

    # Calculate low stock sizes per product
    products_data = []
    for product in products:
        stock = sum(v.total_stock for v in product.variants.all())
        is_low = stock <= product.alert_threshold

        # Find low/zero sizes using SizeAlert thresholds
        low_sizes = []
        zero_sizes = []
        for variant in product.variants.all():
                size_map = {}
                for unit in variant.units.filter(status__in=(ProductUnit.IN_STOCK, ProductUnit.RETURNED)):
                    size_map[unit.size] = size_map.get(unit.size, 0) + 1
                for size, count in size_map.items():
                    threshold = size_alerts.get((variant.pk, size), None)
                    if threshold is not None:
                        if count == 0 and size not in zero_sizes:
                            zero_sizes.append(size)
                        elif count <= threshold and size not in low_sizes and size not in zero_sizes:
                            low_sizes.append(size)

        products_data.append({
            "product": product,
            "stock": stock,
            "low_sizes": low_sizes,
            "zero_sizes": zero_sizes,
            "is_low": is_low,
            "variants": product.variants.all(),
        })

    return render(request, "inventory/products_list.html", {
        "products_data": products_data,
        "products": products,
        "total_available": total_available,
        "total_shipped": total_shipped,
        "total_paid": total_paid,
    })


@login_required(login_url="/login/")
def product_detail(request, pk):
    product = get_object_or_404(Product, pk=pk)
    variants = product.variants.prefetch_related("units").all()

    # Build size breakdown per variant — only available units (in_stock + returned)
    variants_data = []
    for variant in variants:
        # Get ALL sizes that ever existed for this variant (including 0 stock)
        all_sizes = list(variant.units.values_list("size", flat=True).distinct())
        size_map = {}
        for size in all_sizes:
            size_map[size] = 0
        for unit in variant.units.filter(status__in=(ProductUnit.IN_STOCK, ProductUnit.RETURNED)):
            size_map[unit.size] = size_map.get(unit.size, 0) + 1
        variants_data.append({
            "variant": variant,
            "size_map": size_map,
            "total_stock": variant.total_stock,
            "all_units": variant.units.all(),
        })

    return render(request, "inventory/product_detail.html", {
        "product": product,
        "variants": variants,
        "variants_data": variants_data,
    })
