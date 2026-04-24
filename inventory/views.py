import json
from decimal import Decimal
from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse, HttpResponseForbidden
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required

from .models import (
    Product, ProductVariant, ProductUnit,
    ShippingOrder, OrderItem, StockMovement, Payment,
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
    unit_data = {
        "barcode": unit.barcode, "size": unit.size,
        "product_name": variant.product.name, "color_label": variant.color_label,
        "sell_price": str(variant.product.sell_price),
        "image_url": variant.image.url if variant.image else None,
    }
    return unit_data, reconciliation


@csrf_exempt
@require_POST
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
            return JsonResponse({"status": "error", "message": f"Aucun ordre trouvé pour : {barcode}"})
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
        "pending_returns": waiting_return, "total_products": total_products, "overdue_orders": overdue_orders,
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
def products_list(request):
    products = Product.objects.prefetch_related("variants__units").all()
    total_available = ProductUnit.objects.filter(status__in=(ProductUnit.IN_STOCK, ProductUnit.RETURNED)).count()
    total_shipped = ProductUnit.objects.filter(status=ProductUnit.SHIPPED).count()
    total_paid = ProductUnit.objects.filter(status=ProductUnit.PAID).count()
    return render(request, "inventory/products_list.html", {
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
        available_units = variant.units.filter(status__in=(ProductUnit.IN_STOCK, ProductUnit.RETURNED))
        size_map = {}
        for unit in available_units:
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
