import json
import os
from decimal import Decimal
from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse, HttpResponseForbidden
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.db import transaction
from django.db.models import Count, Q
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required

from .models import (
    Product, ProductVariant, ProductUnit,
    ShippingOrder, OrderItem, StockMovement, Payment, SizeAlert, OrderVerification, ScanSessionLog,
)
from .scan_service import handle_shipping_scan, handle_stock_scan


def _user_for_request(request):
    """Return the authenticated user or None."""
    user = getattr(request, 'user', None)
    if user is not None and getattr(user, 'is_authenticated', False):
        return user
    return None


# Navex API URL — token comes from env var, never hard-coded.
# Set NAVEX_API_TOKEN in Railway variables.
NAVEX_API_URL = (
    f"https://app.navex.tn/api/rashop-etat-"
    f"{os.environ.get('NAVEX_API_TOKEN', '')}/v1/post.php"
)


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
    result = handle_shipping_scan(barcode, user=_user_for_request(request))

    # Audit — record what kind of scan this resolved to.
    from .models import log_action, AuditLog
    rtype = result.get("type", "unknown")
    if rtype == "bordereau":
        log_action(
            request.user, AuditLog.SCAN_SHIPPING,
            description=f"Bordereau scanné : {barcode}",
            request=request, target_order_barcode=barcode,
        )
    elif rtype == "unit":
        unit_bc = (result.get("unit") or {}).get("barcode", "")
        order_bc = (result.get("order") or {}).get("bordereau_barcode", "")
        log_action(
            request.user, AuditLog.SCAN_SHIPPING,
            description=f"Unité {unit_bc} scannée vers {order_bc}",
            request=request, target_unit_barcode=unit_bc, target_order_barcode=order_bc,
        )
    elif result.get("status") == "error":
        log_action(
            request.user, AuditLog.SCAN_SHIPPING,
            description=f"Scan refusé : {barcode} — {result.get('message','')[:200]}",
            request=request, target_unit_barcode=barcode,
        )
    return JsonResponse(result)


@login_required(login_url="/login/")
def api_get_order_state(request, pk):
    """Return the current saved unit list for an order — used by the frontend
    to recover after a scan failure or tab focus change. Server is source of truth."""
    try:
        order = ShippingOrder.objects.prefetch_related(
            "items__unit__variant__product"
        ).get(pk=pk)
    except ShippingOrder.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Ordre introuvable."}, status=404)

    units = []
    total = Decimal("0")
    for item in order.items.all():
        unit = item.unit
        if not unit:
            continue
        variant = unit.variant
        product = variant.product
        units.append({
            "barcode": unit.barcode,
            "size": unit.size,
            "status": unit.status,
            "product_name": product.name,
            "color_label": variant.color_label,
            "sell_price": str(product.sell_price),
            "image_url": variant.image.url if variant.image else None,
        })
        total += product.sell_price

    return JsonResponse({
        "status": "ok",
        "order": {
            "id": order.id,
            "bordereau_barcode": order.bordereau_barcode,
            "status": order.status,
            "unit_count": len(units),
            "total": str(total),
        },
        "units": units,
    })


@csrf_exempt
@require_POST
def api_scan_reception(request):
    data = json.loads(request.body)
    barcode = data.get("barcode", "").strip()
    if not barcode:
        return JsonResponse({"status": "error", "message": "Barcode vide."}, status=400)
    result = handle_stock_scan(barcode, user=_user_for_request(request))

    from .models import log_action, AuditLog
    if result.get("status") == "ok":
        unit_bc = (result.get("unit") or {}).get("barcode", barcode)
        log_action(
            request.user, AuditLog.SCAN_RECEPTION,
            description=f"Unité {unit_bc} ajoutée au stock",
            request=request, target_unit_barcode=unit_bc,
        )
    else:
        log_action(
            request.user, AuditLog.SCAN_RECEPTION,
            description=f"Scan stock refusé : {barcode} — {result.get('message','')[:200]}",
            request=request, target_unit_barcode=barcode,
        )
    return JsonResponse(result)


@csrf_exempt
@require_POST
def api_remove_from_order(request):
    """Remove a unit from the currently open order."""
    from .models import log_action, AuditLog
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
        log_action(
            request.user, AuditLog.EDIT,
            description=f"Unité {barcode} retirée de l'ordre ouvert {open_order.bordereau_barcode}",
            request=request,
            target_unit_barcode=barcode,
            target_order_barcode=open_order.bordereau_barcode,
        )
        return JsonResponse({
            "status": "ok",
            "message": f"{barcode} retiré de l'ordre.",
            "unit_count": open_order.items.count(),
        })
    except (ProductUnit.DoesNotExist, OrderItem.DoesNotExist):
        return JsonResponse({"status": "error", "message": f"Unité {barcode} non trouvée dans cet ordre."})


def login_view(request):
    if request.user.is_authenticated:
        return redirect('home')
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        next_url = request.POST.get('next', '/')
        user = authenticate(request, username=username, password=password)
        if user:
            login(request, user)
            return redirect(next_url or 'home')
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
    StockMovement.objects.create(unit=unit, movement_type=StockMovement.RETURNED, reference="RETOUR", user=_user_for_request(request))
    if order_item:
        order_item.status_at_payment = ProductUnit.RETURNED
        order_item.save(update_fields=["status_at_payment"])
    # Update order status based on remaining units (handles paid / livre / closed)
    if order_item and order_item.order_id:
        order_item.order.refresh_from_db()
        _update_order_return_status(order_item.order)
    unit_data = {
        "barcode": unit.barcode, "size": unit.size,
        "product_name": variant.product.name, "color_label": variant.color_label,
        "sell_price": str(variant.product.sell_price),
        "image_url": variant.image.url if variant.image else None,
    }
    return unit_data, reconciliation


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


@csrf_exempt
@require_POST
def api_scan_return(request):
    from .barcode_parser import is_bordereau_barcode
    from .models import log_action, AuditLog
    data = json.loads(request.body)
    barcode = data.get("barcode", "").strip().upper()
    if not barcode:
        return JsonResponse({"status": "error", "message": "Barcode vide."}, status=400)

    if is_bordereau_barcode(barcode):
        try:
            order = ShippingOrder.objects.prefetch_related("items__unit__variant__product").get(bordereau_barcode=barcode)
        except ShippingOrder.DoesNotExist:
            log_action(
                request.user, AuditLog.SCAN_RETURN,
                description=f"Retour : bordereau introuvable {barcode}",
                request=request, target_order_barcode=barcode,
            )
            return JsonResponse({"status": "error", "message": f"Aucun ordre trouvé pour : {barcode}", "code": "ORDER_NOT_FOUND", "barcode": barcode})
        items = order.items.select_related("unit__variant__product")
        # Use the snapshotted status (display_status) — same source the UI uses
        # to show 'Expédié' on each item. This matches user expectation: if the
        # UI says it's expedié, it should be returnable. Also defensive against
        # the rare case where a unit's live status was changed by another flow
        # (e.g. unit reused in another order) leaving the snapshot intact.
        # We also accept legacy items where snapshots are missing (display_status
        # falls back to unit.status).
        RETURNABLE_STATUSES = {"shipped", "paid", "expédié", "expedie"}
        returnable = [i for i in items if (i.display_status or "").strip().lower() in RETURNABLE_STATUSES]
        if not returnable:
            # Build a clearer error message — list what state each item is in
            # so the user understands WHY nothing is returnable.
            if not items:
                msg = "Cet ordre n'a aucun article."
            else:
                # Map statuses to human-readable labels
                status_labels = {
                    "in_stock": "En stock",
                    "shipped": "Expédié",
                    "paid": "Payé",
                    "returned": "Déjà retourné",
                    "defective": "Défectueux",
                }
                item_states = []
                for i in items:
                    s = (i.display_status or "").strip().lower()
                    label = status_labels.get(s, s or "inconnu")
                    item_states.append(f"{i.unit.barcode} : {label}")
                msg = "Aucune unité retournable. État des articles :\n" + "\n".join(item_states)
            return JsonResponse({"status": "error", "message": msg, "code": "NOTHING_RETURNABLE"})
        items_data = [{"barcode": i.unit.barcode, "size": i.unit.size, "status": i.unit.status,
                       "product_name": i.unit.variant.product.name, "color_label": i.unit.variant.color_label,
                       "sell_price": str(i.unit.variant.product.sell_price),
                       "image_url": i.unit.variant.image.url if i.unit.variant.image else None}
                      for i in items]
        if len(returnable) == 1:
            unit_data, reconciliation = _do_return_unit(returnable[0].unit)
            log_action(
                request.user, AuditLog.SCAN_RETURN,
                description=f"Unité {unit_data['barcode']} retournée (auto-single) depuis {barcode}",
                request=request,
                target_unit_barcode=unit_data["barcode"],
                target_order_barcode=barcode,
            )
            return JsonResponse({"status": "ok", "type": "order_single",
                                 "message": f"Unité {unit_data['barcode']} retournée automatiquement.",
                                 "unit": unit_data, "reconciliation": reconciliation})
        log_action(
            request.user, AuditLog.SCAN_RETURN,
            description=f"Retour multi-unité ouvert pour {barcode} ({len(returnable)} retournables)",
            request=request, target_order_barcode=barcode,
        )
        return JsonResponse({"status": "ok", "type": "order_multiple",
                             "order_bordereau": order.bordereau_barcode,
                             "order_id": order.id, "items": items_data})

    try:
        unit = ProductUnit.objects.select_related("variant__product").get(barcode=barcode)
    except ProductUnit.DoesNotExist:
        log_action(
            request.user, AuditLog.SCAN_RETURN,
            description=f"Retour : unité introuvable {barcode}",
            request=request, target_unit_barcode=barcode,
        )
        return JsonResponse({"status": "error", "message": f"Unite introuvable : {barcode}"})
    if unit.status not in (ProductUnit.SHIPPED, ProductUnit.PAID):
        msgs = {
            ProductUnit.IN_STOCK: "déjà en stock",
            ProductUnit.RETURNED: "déjà retournée",
        }
        log_action(
            request.user, AuditLog.SCAN_RETURN,
            description=f"Retour refusé pour {barcode} : statut {unit.get_status_display()}",
            request=request, target_unit_barcode=barcode,
        )
        return JsonResponse({"status": "error", "message": f"Impossible — {msgs.get(unit.status, unit.get_status_display())}."})
    unit_data, reconciliation = _do_return_unit(unit)
    log_action(
        request.user, AuditLog.SCAN_RETURN,
        description=f"Unité {unit_data['barcode']} retournée",
        request=request, target_unit_barcode=unit_data["barcode"],
    )
    return JsonResponse({"status": "ok", "type": "unit_returned",
                         "message": f"{unit_data['product_name']} {unit_data['color_label']} taille {unit_data['size']} retourné.",
                         "unit": unit_data, "reconciliation": reconciliation})


@csrf_exempt
@require_POST
def api_return_multiple(request):
    from .models import log_action, AuditLog
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
    log_action(
        request.user, AuditLog.SCAN_RETURN,
        description=f"Retour multi-unité : {len(returned_units)} unité(s) — {', '.join(u['barcode'] for u in returned_units[:5])}{'...' if len(returned_units) > 5 else ''}",
        request=request,
    )
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
    from .models import log_action, AuditLog
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
                            reference=prev_order.bordereau_barcode, user=_user_for_request(request))
                log_action(
                    request.user, AuditLog.PAYMENT,
                    description=f"Ordre {prev_order.bordereau_barcode} marqué payé (auto, scan suivant)",
                    request=request,
                    target_order_barcode=prev_order.bordereau_barcode,
                    target_model="ShippingOrder", target_id=prev_order.id,
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
    from .models import log_action, AuditLog
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
            StockMovement.objects.create(unit=unit, movement_type=StockMovement.PAID, reference=order.bordereau_barcode, user=_user_for_request(request))
            OrderItem.objects.filter(order=order, unit=unit).update(status_at_payment=ProductUnit.PAID)
        except ProductUnit.DoesNotExist:
            pass

    # Mark refused items as pending_return
    for barcode in refused_barcodes:
        try:
            unit = ProductUnit.objects.get(barcode=barcode)
            unit.status = ProductUnit.RETURNED
            unit.save()
            StockMovement.objects.create(unit=unit, movement_type=StockMovement.RETURNED, reference=order.bordereau_barcode, user=_user_for_request(request))
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
    log_action(
        request.user, AuditLog.PAYMENT,
        description=f"Paiement confirmé pour {order.bordereau_barcode} : {amount_collected} TND ({len(sold_barcodes)} vendu, {pending_count} refusé)",
        request=request,
        target_order_barcode=order.bordereau_barcode,
        target_model="ShippingOrder", target_id=order.id,
    )
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
    from .models import log_action, AuditLog
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
            reference=open_order.bordereau_barcode, user=_user_for_request(request))

    log_action(
        request.user, AuditLog.STATUS_CHANGE,
        description=f"Ordre {open_order.bordereau_barcode} fermé ({open_order.unit_count} unités, {expected_amount} TND)",
        request=request,
        target_order_barcode=open_order.bordereau_barcode,
        target_model="ShippingOrder", target_id=open_order.id,
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
    from .models import log_action, AuditLog
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

    log_action(
        request.user, AuditLog.EDIT,
        description=f"Montant ordre {order.bordereau_barcode} : {old_amount} TND → {amount} TND",
        request=request,
        target_order_barcode=order.bordereau_barcode,
        target_model="ShippingOrder", target_id=order.id,
    )

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
    from .models import log_action, AuditLog
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

    if fixed > 0:
        log_action(
            request.user, AuditLog.EDIT,
            description=f"Resync ordre {order.bordereau_barcode} : {fixed} unité(s) corrigée(s)",
            request=request,
            target_order_barcode=order.bordereau_barcode,
            target_model="ShippingOrder", target_id=order.id,
        )

    return JsonResponse({"status": "ok", "message": f"{fixed} unité(s) resynchronisée(s).", "fixed": fixed})


@csrf_exempt
@require_POST
def api_delete_order(request, pk):
    """Delete an order and return all units to in_stock."""
    from .models import log_action, AuditLog
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
    # Capture key fields BEFORE delete so the audit row stays useful
    bordereau = order.bordereau_barcode
    unit_count = order.items.count()
    order_id = order.id
    # Return all units to in_stock
    for item in order.items.select_related("unit"):
        item.unit.status = ProductUnit.IN_STOCK
        item.unit.save()
        StockMovement.objects.create(
            unit=item.unit, movement_type=StockMovement.RECEIVED,
            reference=f"SUPPRESSION ORDRE {bordereau}", user=_user_for_request(request))
    order.delete()
    log_action(
        request.user, AuditLog.DELETE,
        description=f"Ordre {bordereau} SUPPRIMÉ ({unit_count} unités remises en stock)",
        request=request,
        target_order_barcode=bordereau,
        target_model="ShippingOrder", target_id=order_id,
    )
    return JsonResponse({"status": "ok", "message": "Ordre supprimé, unités remises en stock."})


@csrf_exempt
@require_POST
def api_order_remove_unit(request, pk):
    """Remove a unit from an order and return it to in_stock."""
    from .models import log_action, AuditLog
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
        reference=f"MODIFICATION ORDRE {order.bordereau_barcode}", user=_user_for_request(request))
    log_action(
        request.user, AuditLog.EDIT,
        description=f"Unité {barcode} retirée de l'ordre {order.bordereau_barcode}",
        request=request,
        target_unit_barcode=barcode,
        target_order_barcode=order.bordereau_barcode,
    )
    return JsonResponse({"status": "ok", "message": f"{barcode} retiré et remis en stock."})


@csrf_exempt
@require_POST
def api_order_add_unit(request, pk):
    """Add a unit to an existing order by scanning barcode."""
    from .models import log_action, AuditLog
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
    log_action(
        request.user, AuditLog.EDIT,
        description=f"Unité {barcode} ajoutée à l'ordre {order.bordereau_barcode}",
        request=request,
        target_unit_barcode=barcode,
        target_order_barcode=order.bordereau_barcode,
    )
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
def home_dispatcher(request):
    """Landing page at '/' — admins go to dashboard, everyone else sees the bubble menu."""
    user = request.user
    if user.is_superuser:
        return redirect("dashboard")

    # Determine role (default to office if no profile, e.g. legacy user)
    try:
        role = user.profile.role
    except Exception:
        role = "office"

    return render(request, "inventory/bubble_home.html", {
        "role": role,
        "is_messages_team": role == "messages",
    })


@login_required(login_url="/login/")
def dashboard(request):
    # Determine viewing mode (which bubble was clicked).
    # Admins/superusers always see the full dashboard ("all").
    # Non-admin users without a view param get bounced back to the bubble page.
    view_mode = request.GET.get("view", "")
    if request.user.is_superuser:
        view_mode = view_mode or "all"
    else:
        try:
            role = request.user.profile.role
        except Exception:
            role = "office"
        # Messages Team can only ever see the messages view
        if role == "messages":
            view_mode = "messages"
        # Non-admin without an explicit view → back to bubble picker
        if view_mode not in ("shipping", "office", "messages"):
            return redirect("home")

    # ---- OPTIMIZED: dashboard previously made ~50 queries (one per product
    # for total_stock); now uses a single annotated query.

    # Single SQL: every product with its count of in_stock + returned units
    products_qs = Product.objects.annotate(
        _stock_count=Count(
            "variants__units",
            filter=Q(variants__units__status__in=(ProductUnit.IN_STOCK, ProductUnit.RETURNED)),
        ),
    )

    # Total products (cached from the same query — no extra round-trip)
    total_products = products_qs.count()

    # Low-stock list: filter in Python from the same data we already loaded.
    # Build a small dict per item so the template never re-queries .total_stock.
    products_for_template = list(
        products_qs.filter(archived=False, alert_disabled=False).values(
            "id", "name", "alert_threshold", "_stock_count",
        )
    )
    low_stock = [
        {
            "name": p["name"],
            "total_stock": p["_stock_count"],
            "alert_threshold": p["alert_threshold"],
        }
        for p in products_for_template
        if p["_stock_count"] <= p["alert_threshold"]
    ]

    open_order = ShippingOrder.objects.filter(status=ShippingOrder.OPEN).first()
    recent_orders = ShippingOrder.objects.order_by("-opened_at")[:10]
    now = timezone.now()
    orders_this_month = ShippingOrder.objects.filter(opened_at__year=now.year, opened_at__month=now.month).count()
    total_in_stock = ProductUnit.objects.filter(status=ProductUnit.IN_STOCK).count()
    total_shipped = ProductUnit.objects.filter(status=ProductUnit.SHIPPED).count()

    # Size alerts — skip muted/archived parents
    from .models import SizeAlert
    size_alerts = [
        sa for sa in SizeAlert.objects.select_related("variant__product").all()
        if sa.is_triggered
        and not sa.variant.product.alert_disabled
        and not sa.variant.product.archived
    ]

    # Alerts: shipped units that belong to paid orders = waiting to come back
    waiting_return = ProductUnit.objects.filter(
        status=ProductUnit.SHIPPED,
        order_items__order__status=ShippingOrder.PAID,
    ).distinct().count()
    overdue_orders = [o for o in ShippingOrder.objects.filter(status=ShippingOrder.CLOSED) if o.is_overdue]

    return render(request, "inventory/dashboard.html", {
        # NB: 'products' is no longer passed — the dashboard template doesn't
        # actually iterate every product (it only uses low_stock + size_alerts).
        "open_order": open_order, "recent_orders": recent_orders,
        "low_stock": low_stock, "total_in_stock": total_in_stock,
        "total_shipped": total_shipped, "orders_this_month": orders_this_month,
        "pending_returns": waiting_return, "total_products": total_products,
        "size_alerts": size_alerts, "overdue_orders": overdue_orders,
        "view_mode": view_mode,
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
            NAVEX_API_URL,
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
    from .models import log_action, AuditLog
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
                    reference=order.bordereau_barcode, user=_user_for_request(request))

    log_action(
        request.user, AuditLog.PAYMENT,
        description=f"Ordre {order.bordereau_barcode} marqué payé via Navex — {amount} TND",
        request=request,
        target_order_barcode=order.bordereau_barcode,
        target_model="ShippingOrder", target_id=order.id,
    )

    return JsonResponse({"status": "ok", "message": f"Ordre {order.bordereau_barcode} marque comme paye — {amount} TND."})


@csrf_exempt
def api_navex_en_attente(request):
    """Get all en attente orders from Navex and try to match with our products."""
    import urllib.request, urllib.parse

    try:
        data = urllib.parse.urlencode({"getattente": "1"}).encode()
        req = urllib.request.Request(
            NAVEX_API_URL,
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
                NAVEX_API_URL,
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
    from .models import log_action, AuditLog
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
        try:
            order = ShippingOrder.objects.get(pk=pk)
            log_action(
                request.user, AuditLog.EDIT,
                description=f"Infos Navex enregistrées pour ordre {order.bordereau_barcode} (client: {data.get('nom','')[:60]})",
                request=request,
                target_order_barcode=order.bordereau_barcode,
                target_model="ShippingOrder", target_id=order.id,
            )
        except ShippingOrder.DoesNotExist:
            pass
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
                NAVEX_API_URL,
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
    from .models import log_action, AuditLog
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
    log_action(
        request.user, AuditLog.CREATE,
        description=f"Ordre retour créé : {barcode}",
        request=request,
        target_order_barcode=barcode,
        target_model="ShippingOrder", target_id=order.id,
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
    from .models import log_action, AuditLog
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
            reference=return_order.bordereau_barcode, user=_user_for_request(request))
        
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
    
    log_action(
        request.user, AuditLog.SCAN_RETURN,
        description=f"Unité {barcode} retournée vers ordre retour {return_order.bordereau_barcode}" + (f" (depuis {original_order.bordereau_barcode})" if original_order else ""),
        request=request,
        target_unit_barcode=barcode,
        target_order_barcode=return_order.bordereau_barcode,
    )

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
    """Save a closed order to the daily scan session log.
    Idempotent — same bordereau scanned twice today updates the existing row
    instead of creating a duplicate."""
    data = json.loads(request.body)
    today = timezone.now().date()
    bc = (data.get("bordereau") or "").strip()
    if not bc:
        return JsonResponse({"status": "error", "message": "Bordereau vide."}, status=400)
    ScanSessionLog.objects.update_or_create(
        session_date=today,
        bordereau_barcode=bc,
        defaults={
            "designation": data.get("designation", ""),
            "unit_count": data.get("unit_count", 0),
            "is_correct": data.get("is_correct", True),
            "reason": data.get("reason", ""),
        },
    )
    return JsonResponse({"status": "ok"})


@login_required(login_url="/login/")
def api_get_scan_session(request):
    """Get today's scan session log — dedupes by bordereau (latest wins)."""
    today = timezone.now().date()
    # Order so the *latest* row per bordereau comes first
    logs = ScanSessionLog.objects.filter(session_date=today).order_by("-scanned_at")
    seen = set()
    deduped = []
    for log in logs:
        if log.bordereau_barcode in seen:
            continue
        seen.add(log.bordereau_barcode)
        deduped.append(log)
    return JsonResponse({
        "status": "ok",
        "correct": [{"bc": l.bordereau_barcode, "designation": l.designation, "units": l.unit_count, "time": l.scanned_at.strftime("%H:%M")} for l in deduped if l.is_correct],
        "wrong": [{"bc": l.bordereau_barcode, "designation": l.designation, "units": l.unit_count, "reason": l.reason, "time": l.scanned_at.strftime("%H:%M")} for l in deduped if not l.is_correct],
    })


@login_required(login_url="/login/")
def api_recheck_session(request):
    """Full reconciliation: walk every order closed today, fetch Navex info,
    rebuild the ScanSessionLog rows. Creates missing rows, updates existing
    ones — re-derives is_correct from Navex designation vs scanned products.
    """
    import urllib.request, urllib.parse
    today = timezone.now().date()

    # All orders closed today (any status that comes after CLOSED counts)
    todays_orders = list(
        ShippingOrder.objects
        .filter(closed_at__date=today, status__in=ShippingOrder.CLOSED_STATUSES)
        .prefetch_related("items__unit__variant__product")
    )

    if not todays_orders:
        return JsonResponse({"status": "ok", "checked": 0, "created": 0, "updated": 0})

    barcodes = [o.bordereau_barcode for o in todays_orders]

    # Bulk-fetch Navex status + prix
    navex_etat = {}
    navex_prix = {}
    try:
        codes_string = ", ".join(barcodes)
        data = urllib.parse.urlencode({"codes": codes_string, "include_prix": "1"}).encode()
        req = urllib.request.Request(NAVEX_API_URL, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=15) as resp:
            navex_data = json.loads(resp.read().decode())
        for result in navex_data.get("results", []):
            if result.get("status") == 1:
                navex_etat[result["code"]] = result.get("etat", "")
                navex_prix[result["code"]] = result.get("prix")
    except Exception:
        return JsonResponse({"status": "error", "message": "Navex indisponible."})

    # Bulk-fetch designations from getattente (those in en-attente still have it)
    navex_designation = {}
    try:
        data = urllib.parse.urlencode({"getattente": "1"}).encode()
        req = urllib.request.Request(NAVEX_API_URL, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=15) as resp:
            attente = json.loads(resp.read().decode())
        for colis in attente.get("colis", []):
            navex_designation[colis.get("code_barre", "")] = colis.get("designation", "")
    except Exception:
        # Designation lookup is best-effort — skip if Navex burps
        pass

    all_products = list(Product.objects.all())
    created = 0
    updated = 0

    for order in todays_orders:
        bc = order.bordereau_barcode
        etat = navex_etat.get(bc, "INTROUVABLE")
        prix_navex = navex_prix.get(bc)
        # Prefer designation we already saved on the order, fall back to Navex en-attente
        designation = order.navex_designation or navex_designation.get(bc, "")

        scanned_names = list(
            order.items.values_list("unit__variant__product__name", flat=True)
        )
        scanned_lower = [n.lower() for n in scanned_names if n]

        reasons = []

        if etat == "INTROUVABLE":
            reasons.append("Introuvable sur Navex — possible annulation")
        elif etat in ("Annulé", "Annule", "Annulée"):
            reasons.append("Annulé sur Navex")

        # Price mismatch (only meaningful when we have both sides)
        if prix_navex and order.amount_collected:
            try:
                diff = abs(float(prix_navex) - float(order.amount_collected))
                if diff > 0.1:
                    reasons.append(
                        f"Prix différent — Navex: {prix_navex} TND / Notre: {order.amount_collected} TND"
                    )
            except (TypeError, ValueError):
                pass

        # Designation vs scanned products
        if designation:
            items_in_desig = [part.strip() for part in designation.split(",")]
            if items_in_desig and "|" in items_in_desig[0]:
                items_in_desig[0] = items_in_desig[0].split("|", 1)[1].strip()

            expected = []
            for item in items_in_desig:
                item_lower = item.lower()
                for product in all_products:
                    if product.name.lower() in item_lower:
                        expected.append(product.name.lower())
                        break

            missing = []
            for exp in expected:
                first_word = exp.split()[0] if exp.split() else exp
                if not any(first_word in s for s in scanned_lower):
                    missing.append(exp)
            if missing:
                reasons.append(f"Produits manquants: {', '.join(missing)}")

        is_correct = not reasons
        reason_text = " | ".join(reasons)

        # Upsert today's log row for this barcode
        log = ScanSessionLog.objects.filter(
            session_date=today, bordereau_barcode=bc
        ).first()
        if log:
            log.designation = designation
            log.unit_count = len(scanned_names)
            log.is_correct = is_correct
            log.reason = reason_text
            log.save(update_fields=["designation", "unit_count", "is_correct", "reason"])
            updated += 1
        else:
            ScanSessionLog.objects.create(
                bordereau_barcode=bc,
                designation=designation,
                unit_count=len(scanned_names),
                is_correct=is_correct,
                reason=reason_text,
                session_date=today,
            )
            created += 1

    # Return counts that match what /api/scan-session/today/ returns
    # (deduped by bordereau, only logs for today). This ensures the Recheck
    # button shows the same number as CORRECTS in the UI.
    today_logs = ScanSessionLog.objects.filter(session_date=today).order_by("-scanned_at")
    seen = set()
    correct_count = 0
    wrong_count = 0
    for log in today_logs:
        if log.bordereau_barcode in seen:
            continue
        seen.add(log.bordereau_barcode)
        if log.is_correct:
            correct_count += 1
        else:
            wrong_count += 1

    return JsonResponse({
        "status": "ok",
        "checked": correct_count,  # what the button displays — matches CORRECTS
        "correct": correct_count,
        "wrong": wrong_count,
        "created": created,
        "updated": updated,
    })


import resend as resend_client

import socket as _socket

# Configure Resend client from env (never hard-code the key)
resend_client.api_key = os.environ.get("RESEND_API_KEY", "")

# Resend free plan only delivers to verified addresses.
# Until a domain is verified, send everything to this single recipient.
EMAIL_RECIPIENTS = ["ibrahimrahmen0@gmail.com"]
EMAIL_FROM = "Stock Manager <onboarding@resend.dev>"


def _send_email(subject, body):
    """Send a plain-text email via Resend. Returns True on success, False otherwise."""
    if not resend_client.api_key:
        # No key configured (e.g. local dev) — silently no-op instead of crashing.
        return False
    try:
        resend_client.Emails.send({
            "from": EMAIL_FROM,
            "to": EMAIL_RECIPIENTS,
            "subject": subject,
            "text": body,
        })
        return True
    except Exception:
        # Match previous fail_silently behaviour
        return False

def _send_low_stock_email():
    """Send low stock report email — predictive (size will run out in <10 days).
    Skips products with alert_disabled=True or archived=True."""
    from .models import compute_size_forecast, ALERT_DAYS
    products = Product.objects.filter(
        alert_disabled=False, archived=False
    ).prefetch_related("variants__units")
    low_items = []
    for product in products:
        stock = sum(v.total_stock for v in product.variants.all())
        if stock <= product.alert_threshold:
            low_items.append({
                "name": product.name, "code": product.code,
                "stock": stock, "info": f"seuil produit: {product.alert_threshold}",
            })
        # Check predictive per-size alerts
        for variant in product.variants.all():
            sizes = set(variant.units.values_list("size", flat=True).distinct())
            for size in sizes:
                f = compute_size_forecast(variant, size)
                if f["is_triggered"] and f["current_stock"] > 0:
                    if f["days_of_cover"] is not None:
                        info = f"~{f['days_of_cover']}j restants à {f['daily_rate']}/jour"
                    else:
                        info = "stock 0"
                    low_items.append({
                        "name": f"{product.name} {variant.color_label} taille {size}",
                        "code": product.code,
                        "stock": f["current_stock"],
                        "info": info,
                    })

    if not low_items:
        return False

    lines = "\n".join(
        f"- {item['name']} ({item['code']}): {item['stock']} unités ({item['info']})"
        for item in low_items
    )
    body = f"""Rapport de stock bas — {timezone.now().strftime('%d/%m/%Y %H:%M')}

Les produits suivants ont un stock bas (alerte si moins de {ALERT_DAYS} jours de couverture) :

{lines}

Connectez-vous pour voir les détails : https://web-production-1391c5.up.railway.app/products/
"""
    return _send_email(
        subject=f"⚠ Stock bas — {len(low_items)} produit(s) à réapprovisionner",
        body=body,
    )


def _send_daily_summary_email():
    """Send daily scan summary email."""
    today = timezone.now().date()
    # Today's session — dedupe per bordereau (latest row per barcode wins)
    from .models import ScanSessionLog
    logs = ScanSessionLog.objects.filter(session_date=today).order_by("-scanned_at")
    seen = set()
    correct = 0
    wrong = 0
    for log in logs:
        if log.bordereau_barcode in seen:
            continue
        seen.add(log.bordereau_barcode)
        if log.is_correct:
            correct += 1
        else:
            wrong += 1

    # En attente from Navex (approx from our DB)
    import urllib.request, urllib.parse
    navex_count = "N/A"
    try:
        data = urllib.parse.urlencode({"getattente": "1"}).encode()
        req = urllib.request.Request(
            NAVEX_API_URL,
            data=data, method="POST"
        )
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=10) as resp:
            import json as json_lib
            navex_data = json_lib.loads(resp.read().decode())
        our_barcodes = set(ShippingOrder.objects.filter(status__in=(ShippingOrder.CLOSED, ShippingOrder.PAID)).values_list("bordereau_barcode", flat=True))
        navex_count = sum(1 for c in navex_data.get("colis", []) if c.get("code_barre") not in our_barcodes)
    except Exception:
        pass

    # À vérifier count
    from .models import OrderVerification
    now = timezone.now()
    verifier_count = 0
    for order in ShippingOrder.objects.filter(status__in=(ShippingOrder.CLOSED, ShippingOrder.PARTIAL_RETURNED)):
        if order.closed_at:
            weekday = order.closed_at.weekday()
            limit = 58 if weekday == 5 else 34
            if (now - order.closed_at).total_seconds() / 3600 >= limit:
                verifier_count += 1

    body = f"""Résumé de la journée — {today.strftime('%d/%m/%Y')}

📦 Ordres scannés aujourd'hui :
  ✓ Corrects : {correct}
  ✗ À vérifier : {wrong}

⏳ En attente Navex (non encore scannés) : {navex_count}

⚠ Ordres en retard de livraison : {verifier_count}

Connectez-vous pour voir les détails : https://web-production-1391c5.up.railway.app/
"""
    return _send_email(
        subject=f"📊 Résumé du {today.strftime('%d/%m/%Y')} — {correct} ordres scannés",
        body=body,
    )


def _send_a_verifier_email():
    """Send À vérifier alert email."""
    now = timezone.now()
    late_orders = []
    for order in ShippingOrder.objects.filter(status__in=(ShippingOrder.CLOSED, ShippingOrder.PARTIAL_RETURNED)):
        if order.closed_at:
            weekday = order.closed_at.weekday()
            limit = 58 if weekday == 5 else 34
            hours = (now - order.closed_at).total_seconds() / 3600
            if hours >= limit:
                late_orders.append({"bc": order.bordereau_barcode, "hours": round(hours, 1), "closed": order.closed_at.strftime("%d/%m/%Y %H:%M")})

    if not late_orders:
        return False

    lines = "\n".join([f"- {o['bc']} — expédié le {o['closed']} ({o['hours']}h sans livraison)" for o in late_orders])
    body = f"""Alerte — Ordres en retard de livraison — {now.strftime('%d/%m/%Y %H:%M')}

{len(late_orders)} ordre(s) expédiés depuis plus de 34h sans confirmation de livraison :

{lines}

Vérifiez ces ordres : https://web-production-1391c5.up.railway.app/a-verifier/
"""
    return _send_email(
        subject=f"⚠ {len(late_orders)} ordre(s) en retard de livraison",
        body=body,
    )


# Manual email send endpoints
@csrf_exempt
def api_send_email(request, email_type):
    if not request.user.is_superuser:
        return JsonResponse({"status": "error", "message": "Admin uniquement."})
    
    try:
        if email_type == "low_stock":
            result = _send_low_stock_email()
            msg = "Email stock bas envoyé !" if result else "Aucun produit en stock bas."
        elif email_type == "daily_summary":
            result = _send_daily_summary_email()
            msg = "Résumé quotidien envoyé !"
        elif email_type == "a_verifier":
            result = _send_a_verifier_email()
            msg = "Email À vérifier envoyé !" if result else "Aucun ordre en retard."
        else:
            return JsonResponse({"status": "error", "message": "Type inconnu."})
        return JsonResponse({"status": "ok", "message": msg})
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)})


# Cron endpoints (called by Railway cron)
def cron_morning_email(request):
    """Called at 11am by Railway cron."""
    _send_low_stock_email()
    return JsonResponse({"status": "ok", "type": "morning"})


def cron_evening_email(request):
    """Called at 7pm by Railway cron."""
    _send_daily_summary_email()
    _send_a_verifier_email()
    return JsonResponse({"status": "ok", "type": "evening"})


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
    from .models import log_action, AuditLog
    import urllib.request
    import urllib.parse

    # Get all shipped/closed order barcodes
    # Only check CLOSED orders — not yet paid in our system
    orders = ShippingOrder.objects.filter(
        status=ShippingOrder.CLOSED
    ).values("id", "bordereau_barcode", "status", "amount_collected", "opened_at")

    if not orders:
        return JsonResponse({"status": "ok", "results": []})

    log_action(
        request.user, AuditLog.NAVEX_SYNC,
        description=f"Sync Navex lancé sur {len(orders)} ordre(s) CLOSED",
        request=request,
    )

    codes = [o["bordereau_barcode"] for o in orders]
    codes_string = ", ".join(codes)

    try:
        data = urllib.parse.urlencode({"codes": codes_string, "include_prix": "1"}).encode()
        req = urllib.request.Request(
            NAVEX_API_URL,
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
    show_archived = request.GET.get("archived") == "1"
    products_qs = Product.objects.prefetch_related("variants__units")
    if show_archived:
        products_qs = products_qs.filter(archived=True)
    else:
        products_qs = products_qs.filter(archived=False)
    products = products_qs.all()
    total_available = ProductUnit.objects.filter(status__in=(ProductUnit.IN_STOCK, ProductUnit.RETURNED)).count()
    total_shipped = ProductUnit.objects.filter(status=ProductUnit.SHIPPED).count()
    total_paid = ProductUnit.objects.filter(status=ProductUnit.PAID).count()

    from .models import compute_size_forecast

    # Calculate low stock sizes per product (predictive: days-of-cover < 10)
    products_data = []
    for product in products:
        stock = sum(v.total_stock for v in product.variants.all())
        is_low = stock <= product.alert_threshold and not product.alert_disabled

        # Find low/zero sizes using predictive forecast — skipped entirely for muted products
        low_sizes = []
        zero_sizes = []
        if not product.alert_disabled:
            for variant in product.variants.all():
                size_map = {}
                for unit in variant.units.filter(status__in=(ProductUnit.IN_STOCK, ProductUnit.RETURNED)):
                    size_map[unit.size] = size_map.get(unit.size, 0) + 1
                # Also include sizes with units in any status, so we can flag zero stock
                all_sizes = set(size_map.keys()) | set(
                    variant.units.values_list("size", flat=True).distinct()
                )
                for size in all_sizes:
                    count = size_map.get(size, 0)
                    if count == 0:
                        if size not in zero_sizes:
                            zero_sizes.append(size)
                    else:
                        forecast = compute_size_forecast(variant, size)
                        if forecast["is_triggered"] and size not in low_sizes:
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
        "show_archived": show_archived,
        "archived_count": Product.objects.filter(archived=True).count(),
    })


@login_required(login_url="/login/")
def product_detail(request, pk):
    product = get_object_or_404(Product, pk=pk)
    from .models import compute_size_forecast
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

        # Predictive forecast per size — feeds the alert UI
        size_forecasts = {}
        for size in size_map.keys():
            size_forecasts[size] = compute_size_forecast(variant, size)

        variants_data.append({
            "variant": variant,
            "size_map": size_map,
            "size_forecasts": size_forecasts,
            "total_stock": variant.total_stock,
            "all_units": variant.units.all(),
        })

    return render(request, "inventory/product_detail.html", {
        "product": product,
        "variants": variants,
        "variants_data": variants_data,
    })


@csrf_exempt
@require_POST
@login_required(login_url="/login/")
def api_toggle_product_flag(request, pk):
    """Toggle alert_disabled or archived on a product. Admin-only."""
    if not request.user.is_superuser:
        return JsonResponse({"status": "error", "message": "Accès refusé."}, status=403)
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "JSON invalide."}, status=400)
    flag = data.get("flag")
    if flag not in ("alert_disabled", "archived"):
        return JsonResponse({"status": "error", "message": "Flag invalide."}, status=400)
    product = get_object_or_404(Product, pk=pk)
    old_value = getattr(product, flag)
    new_value = bool(data.get("value", not old_value))
    setattr(product, flag, new_value)
    product.save(update_fields=[flag])

    from .models import log_action, AuditLog
    log_action(
        request.user, AuditLog.EDIT,
        description=f"Produit '{product.name}' : {flag} {old_value} → {new_value}",
        request=request,
        target_model="Product", target_id=product.id,
    )

    return JsonResponse({
        "status": "ok",
        "flag": flag,
        "value": new_value,
        "alert_disabled": product.alert_disabled,
        "archived": product.archived,
    })


@login_required(login_url="/login/")
def api_check_barcode_gaps(request, pk):
    """Find gaps in unit barcode numeric sequences, grouped by (color, size).

    Barcode pattern: <PREFIX>-<COLOR>-<SIZE>-<NUMBER>  e.g. PTSICY-WTE-3-016
    For each (color, size) group we extract the trailing number and report
    any missing integers between min and max.
    """
    import re
    product = get_object_or_404(Product, pk=pk)

    # Optional ?variant=ID & ?size=N filtering for per-variant button
    variant_id = request.GET.get("variant")
    size_filter = request.GET.get("size")

    variants = product.variants.prefetch_related("units").all()
    if variant_id:
        variants = variants.filter(pk=variant_id)

    groups = []  # one dict per (variant, size)
    for variant in variants:
        # Group units by size
        size_buckets = {}
        for unit in variant.units.all():
            if size_filter and str(unit.size) != str(size_filter):
                continue
            size_buckets.setdefault(unit.size, []).append(unit.barcode)

        for size, barcodes in size_buckets.items():
            numbers = []
            for bc in barcodes:
                m = re.search(r"(\d+)$", bc or "")
                if m:
                    numbers.append(int(m.group(1)))
            if not numbers:
                continue
            numbers_set = set(numbers)
            lo, hi = min(numbers), max(numbers)
            missing = [n for n in range(lo, hi + 1) if n not in numbers_set]

            groups.append({
                "variant_id": variant.pk,
                "color": variant.color_label,
                "size": size,
                "count": len(numbers),
                "range_lo": lo,
                "range_hi": hi,
                "missing": missing,
                "missing_count": len(missing),
            })

    # Sort: variants by color, then by size
    groups.sort(key=lambda g: (g["color"], str(g["size"])))

    total_missing = sum(g["missing_count"] for g in groups)
    return JsonResponse({
        "status": "ok",
        "product": product.name,
        "total_missing": total_missing,
        "groups": groups,
    })


# ---------------------------------------------------------------------------
# V2 — ORDER MANAGEMENT (Phase 4)
# Customer order creation. Independent from the existing scan/Navex flow.
# Once an order is "Confirmée", a later phase will push it to Navex and
# fill in the bordereau_barcode.
# ---------------------------------------------------------------------------

def _orders_role_check(request):
    """Office, Shipping, and Admins may access. Messages Team cannot."""
    if not request.user.is_authenticated:
        return False
    if request.user.is_superuser:
        return True
    try:
        return request.user.profile.role != "messages"
    except Exception:
        return True  # legacy users default to allowed


@login_required(login_url="/login/")
def orders_list(request):
    if not _orders_role_check(request):
        return redirect("home")
    from .models import Order, SalesPage, Region
    status_filter = request.GET.get("status", "")
    qs = Order.objects.select_related("customer", "region", "sales_page").prefetch_related(
        "lines__product", "order_offers"
    )
    if status_filter:
        qs = qs.filter(status=status_filter)
    orders = qs[:500]

    from django.db.models import Count
    counts = dict(Order.objects.values_list("status").annotate(n=Count("id")))
    return render(request, "inventory/orders_list.html", {
        "orders": orders,
        "status_filter": status_filter,
        "counts": counts,
        "total": Order.objects.count(),
        # Data needed by the inline-create row + modal
        "sales_pages": SalesPage.objects.filter(is_active=True),
        "regions": Region.objects.filter(is_active=True),
    })


@login_required(login_url="/login/")
def order_create(request):
    """Legacy alias — redirects to orders list (inline-create lives there)."""
    return redirect("orders_list")


# ---- New inline-create flow APIs ------------------------------------------

@login_required(login_url="/login/")
def api_offers_for_page(request, page_id):
    """Return active offers attached to a given SalesPage."""
    if not _orders_role_check(request):
        return JsonResponse({"status": "error"}, status=403)
    from .models import Offer
    offers = Offer.objects.filter(is_active=True, sales_pages__id=page_id).distinct()
    return JsonResponse({
        "status": "ok",
        "offers": [
            {"id": o.id, "name": o.name, "bundle_price": str(o.bundle_price)}
            for o in offers
        ],
    })


@login_required(login_url="/login/")
def api_offer_detail(request, offer_id):
    """Return an offer's products with each product's variants and sizes-with-stock."""
    if not _orders_role_check(request):
        return JsonResponse({"status": "error"}, status=403)
    from .models import Offer
    try:
        offer = Offer.objects.prefetch_related("products__product__variants__units").get(pk=offer_id)
    except Offer.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Offre introuvable."}, status=404)

    products_data = []
    for op in offer.products.all():
        product = op.product
        variants = []
        for v in product.variants.all():
            # stock_by_size = how many in_stock or returned units per size
            stock_by_size = {}
            sizes_seen = []
            for u in v.units.all():
                if u.status in (ProductUnit.IN_STOCK, ProductUnit.RETURNED):
                    stock_by_size[u.size] = stock_by_size.get(u.size, 0) + 1
                if u.size and u.size not in sizes_seen:
                    sizes_seen.append(u.size)
            variants.append({
                "id": v.id,
                "color": v.color_label or v.color_name,
                "sizes": sizes_seen,
                "stock_by_size": stock_by_size,
            })
        products_data.append({
            "offer_product_id": op.id,
            "product_id": product.id,
            "product_name": product.name,
            "quantity": op.quantity,
            "variants": variants,
        })

    return JsonResponse({
        "status": "ok",
        "offer": {
            "id": offer.id, "name": offer.name,
            "bundle_price": str(offer.bundle_price),
            "products": products_data,
        },
    })


@csrf_exempt
@require_POST
@login_required(login_url="/login/")
def api_create_order_inline(request):
    """Save a new order from the inline-row form.

    Expected JSON payload:
    {
      "phone": "...", "name": "...",
      "sales_page": <id>, "region": <id>,
      "ville": "...", "localite": "...", "address": "...",
      "delivery_fee": 7, "discount": 0, "notes": "...",
      "offers": [
        {
          "offer_id": 12, "quantity": 1,
          "products": [
            {"offer_product_id": 5, "product_id": 3, "variant_id": 8, "size": "M", "quantity": 1},
            ...
          ]
        }, ...
      ]
    }
    """
    from .models import (
        Order, OrderLine, OrderOffer, Offer, Customer, SalesPage, Region,
        log_action, AuditLog,
    )
    if not _orders_role_check(request):
        return JsonResponse({"status": "error", "message": "Accès refusé."}, status=403)
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "JSON invalide."}, status=400)

    try:
        phone = (data.get("phone") or "").strip()
        if not phone:
            return JsonResponse({"status": "error", "message": "Téléphone obligatoire."}, status=400)

        sales_page_id = data.get("sales_page")
        if not sales_page_id:
            return JsonResponse({"status": "error", "message": "Page obligatoire."}, status=400)

        offers_payload = data.get("offers", [])
        if not offers_payload:
            return JsonResponse({"status": "error", "message": "Au moins une offre requise."}, status=400)

        name = (data.get("name") or "").strip()
        customer, _ = Customer.objects.get_or_create(phone=phone, defaults={"name": name})
        if name and customer.name != name:
            customer.name = name
            customer.save(update_fields=["name"])

        delivery_fee = Decimal(str(data.get("delivery_fee") or "7"))
        discount = Decimal(str(data.get("discount") or "0"))

        with transaction.atomic():
            order = Order.objects.create(
                customer=customer,
                sales_page_id=sales_page_id,
                region_id=data.get("region") or None,
                ville=(data.get("ville") or "").strip(),
                localite=(data.get("localite") or "").strip(),
                address=(data.get("address") or "").strip(),
                delivery_fee=delivery_fee,
                discount=discount,
                notes=(data.get("notes") or "").strip(),
                created_by=request.user if request.user.is_authenticated else None,
            )

            for op in offers_payload:
                try:
                    offer = Offer.objects.get(pk=op.get("offer_id"))
                except Offer.DoesNotExist:
                    continue
                qty = max(int(op.get("quantity") or 1), 1)
                order_offer = OrderOffer.objects.create(
                    order=order, offer=offer,
                    offer_name=offer.name,
                    bundle_price=offer.bundle_price,
                    quantity=qty,
                )
                for line in op.get("products", []):
                    OrderLine.objects.create(
                        order=order,
                        order_offer=order_offer,
                        product_id=line.get("product_id"),
                        variant_id=line.get("variant_id") or None,
                        size=(line.get("size") or "").strip(),
                        quantity=max(int(line.get("quantity") or 1), 1),
                        unit_price=0,  # individual product price not used inside an offer
                    )

            order.recalc_total()

        log_action(
            request.user, AuditLog.CREATE,
            description=f"Commande #{order.id} créée pour {customer} ({order.order_offers.count()} offre(s), {order.total} TND)",
            request=request,
            target_model="Order", target_id=order.id,
        )
        return JsonResponse({
            "status": "ok",
            "order": {
                "id": order.id,
                "total": str(order.total),
                "redirect": f"/orders/{order.id}/",
            },
        })
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)}, status=400)


# ---- Admin-only: manage offers ---------------------------------------------

def _admin_only(view_fn):
    from functools import wraps
    @wraps(view_fn)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("login")
        if not request.user.is_superuser:
            return redirect("home")
        return view_fn(request, *args, **kwargs)
    return wrapper


@_admin_only
def offers_manage(request):
    """Custom admin page to manage offers."""
    from .models import Offer, SalesPage, Product
    return render(request, "inventory/offers_manage.html", {
        "offers": Offer.objects.prefetch_related("sales_pages", "products__product").all(),
        "sales_pages": SalesPage.objects.filter(is_active=True),
        "products": Product.objects.filter(archived=False).order_by("name"),
    })


@csrf_exempt
@require_POST
@_admin_only
def api_offer_create(request):
    from .models import Offer, SalesPage, Product, OfferProduct, log_action, AuditLog
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "JSON invalide."}, status=400)
    name = (data.get("name") or "").strip()
    if not name:
        return JsonResponse({"status": "error", "message": "Nom obligatoire."}, status=400)
    if Offer.objects.filter(name=name).exists():
        return JsonResponse({"status": "error", "message": "Une offre avec ce nom existe déjà."}, status=400)
    bundle_price = Decimal(str(data.get("bundle_price") or "0"))
    page_ids = data.get("sales_page_ids") or []
    products_data = data.get("products") or []  # list of {product_id, quantity}

    with transaction.atomic():
        offer = Offer.objects.create(name=name, bundle_price=bundle_price)
        if page_ids:
            offer.sales_pages.set(SalesPage.objects.filter(id__in=page_ids))
        for p in products_data:
            try:
                OfferProduct.objects.create(
                    offer=offer,
                    product_id=int(p.get("product_id")),
                    quantity=max(int(p.get("quantity") or 1), 1),
                )
            except Exception:
                pass

    log_action(
        request.user, AuditLog.CREATE,
        description=f"Offre créée : '{offer.name}' à {offer.bundle_price} DT",
        request=request,
        target_model="Offer", target_id=offer.id,
    )
    return JsonResponse({"status": "ok", "offer_id": offer.id})


@csrf_exempt
@require_POST
@_admin_only
def api_offer_update(request, pk):
    from .models import Offer, SalesPage, Product, OfferProduct, log_action, AuditLog
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "JSON invalide."}, status=400)
    try:
        offer = Offer.objects.get(pk=pk)
    except Offer.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Offre introuvable."}, status=404)

    with transaction.atomic():
        if "name" in data:
            offer.name = data["name"].strip() or offer.name
        if "bundle_price" in data:
            offer.bundle_price = Decimal(str(data["bundle_price"]))
        if "is_active" in data:
            offer.is_active = bool(data["is_active"])
        offer.save()
        if "sales_page_ids" in data:
            offer.sales_pages.set(SalesPage.objects.filter(id__in=data["sales_page_ids"]))
        if "products" in data:
            offer.products.all().delete()
            for p in data["products"]:
                try:
                    OfferProduct.objects.create(
                        offer=offer,
                        product_id=int(p.get("product_id")),
                        quantity=max(int(p.get("quantity") or 1), 1),
                    )
                except Exception:
                    pass

    log_action(
        request.user, AuditLog.EDIT,
        description=f"Offre modifiée : '{offer.name}'",
        request=request,
        target_model="Offer", target_id=offer.id,
    )
    return JsonResponse({"status": "ok"})


@csrf_exempt
@require_POST
@_admin_only
def api_offer_delete(request, pk):
    from .models import Offer, log_action, AuditLog
    try:
        offer = Offer.objects.get(pk=pk)
    except Offer.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Offre introuvable."}, status=404)
    name = offer.name
    offer.delete()
    log_action(
        request.user, AuditLog.DELETE,
        description=f"Offre supprimée : '{name}'",
        request=request,
        target_model="Offer", target_id=pk,
    )
    return JsonResponse({"status": "ok"})


@login_required(login_url="/login/")
def order_view(request, pk):
    if not _orders_role_check(request):
        return redirect("home")
    from .models import Order
    order = get_object_or_404(
        Order.objects.select_related("customer", "region", "sales_page", "created_by")
                     .prefetch_related("lines__product", "lines__variant"),
        pk=pk,
    )
    return render(request, "inventory/order_view.html", {"order": order})


@csrf_exempt
@require_POST
@login_required(login_url="/login/")
def api_order_change_status(request, pk):
    """Change the status of an order, enforcing valid transitions.

    Rules:
    - Once an order has a Navex barcode, the only allowed status change is to ANNULEE.
    - Setting status to CONFIRMEE auto-triggers a Navex push. If the push fails,
      status stays where it was and the error is returned.
    - Cancellation requires a `cancel_reason`. For 'rupture_stock', the system
      verifies the stock and refuses if the products are actually available.
    """
    if not _orders_role_check(request):
        return JsonResponse({"status": "error", "message": "Accès refusé."}, status=403)
    from .models import Order, log_action, AuditLog
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "JSON invalide."}, status=400)

    new_status = data.get("status", "")
    cancel_reason = (data.get("cancel_reason") or "").strip()
    valid = dict(Order.STATUS_CHOICES)
    if new_status not in valid:
        return JsonResponse({"status": "error", "message": "Statut invalide."}, status=400)

    order = get_object_or_404(Order, pk=pk)
    old_status = order.status
    old_label = order.get_status_display()

    # ---- Lock: if an order has a Navex barcode, only annulation is allowed ----
    if order.bordereau_barcode and new_status != Order.ANNULEE:
        return JsonResponse({
            "status": "error",
            "message": "Cette commande a déjà été envoyée à Navex. Seule une annulation est possible.",
            "code": "LOCKED_BY_NAVEX",
        }, status=400)

    # ---- Confirmée → auto-push to Navex ----
    if new_status == Order.CONFIRMEE:
        if order.bordereau_barcode:
            return JsonResponse({
                "status": "error",
                "message": f"Déjà confirmée (bordereau {order.bordereau_barcode}).",
            }, status=400)
        # Delegate to the existing push function via internal call
        # Re-use the logic from api_push_order_to_navex
        return _push_order_to_navex_internal(request, order)

    # ---- Annulée → require a reason ----
    if new_status == Order.ANNULEE:
        valid_reasons = [Order.CANCEL_CLIENT, Order.CANCEL_CHANGEMENT, Order.CANCEL_RUPTURE]
        if cancel_reason not in valid_reasons:
            return JsonResponse({
                "status": "error",
                "message": "Raison d'annulation requise (client, changement ou rupture_stock).",
                "code": "REASON_REQUIRED",
            }, status=400)

        # Pre-cancel re-check: if a Navex bordereau exists, fetch the latest status
        # before doing anything. This catches cases where Navex moved the colis to
        # a non-cancellable state since our last sync (e.g. 'En magasin').
        if order.bordereau_barcode:
            _sync_navex_status_for_order(order)
            order.refresh_from_db()
            # Soft check: if we have a previously-known terminal/locked status,
            # refuse before even calling the cancel endpoint. We don't hardcode
            # which statuses lock — Navex's delete API will reject impossible
            # cancellations on its own. We just surface the latest status to the
            # user so they understand if it fails.

        # Rupture de stock: verify the products are actually out
        if cancel_reason == Order.CANCEL_RUPTURE:
            has_rupture, details = _check_order_stock_rupture(order)
            if not has_rupture:
                return JsonResponse({
                    "status": "error",
                    "message": "Les produits sont disponibles en stock — pas de rupture confirmée.",
                    "code": "NOT_RUPTURE",
                    "stock_details": details,
                }, status=400)

        # If a Navex bordereau exists, we MUST cancel it on Navex first.
        navex_was_cancelled = False
        navex_response = None
        if order.bordereau_barcode:
            ok, navex_response = _navex_cancel_colis(order.bordereau_barcode)
            if not ok:
                # Cancellation on Navex failed → don't change anything in our DB
                err_msg = ""
                if isinstance(navex_response, dict):
                    err_msg = navex_response.get("status_message") or \
                              navex_response.get("message") or \
                              navex_response.get("_error") or \
                              navex_response.get("_raw") or ""
                log_action(
                    request.user, AuditLog.OTHER,
                    description=f"Annulation refusée par Navex pour #{order.id} (bordereau {order.bordereau_barcode}): {str(navex_response)[:200]}",
                    request=request, target_model="Order", target_id=order.id,
                    extra=str(navex_response)[:5000],
                )
                return JsonResponse({
                    "status": "error",
                    "message": f"Navex a refusé la suppression : {err_msg or 'erreur inconnue'}. La commande n'a pas été annulée.",
                    "code": "NAVEX_CANCEL_FAILED",
                    "navex_response": navex_response,
                }, status=400)
            navex_was_cancelled = True
            log_action(
                request.user, AuditLog.OTHER,
                description=f"Bordereau {order.bordereau_barcode} supprimé sur Navex (commande #{order.id})",
                request=request, target_model="Order", target_id=order.id,
                extra=str(navex_response)[:5000],
            )

        # Branch: "Annulé pour changement" REVERTS the order to non_confirmee + editable
        if cancel_reason == Order.CANCEL_CHANGEMENT:
            old_bordereau = order.bordereau_barcode
            order.status = Order.NON_CONFIRMEE
            order.bordereau_barcode = ""
            order.navex_label_url = ""
            order.pushed_to_navex_at = None
            # Don't set cancel_reason or cancelled_at — order is "live" again
            order.save(update_fields=[
                "status", "bordereau_barcode", "navex_label_url",
                "pushed_to_navex_at", "updated_at",
            ])
            log_action(
                request.user, AuditLog.STATUS_CHANGE,
                description=f"Commande #{order.id} ré-ouverte pour modification (ancien bordereau {old_bordereau} supprimé sur Navex)",
                request=request, target_model="Order", target_id=order.id,
            )
            return JsonResponse({
                "status": "ok",
                "new_status": Order.NON_CONFIRMEE,
                "label": "Non confirmée",
                "reopened": True,
                "navex_was_cancelled": navex_was_cancelled,
                "redirect": f"/sales-orders/{order.id}/",
            })

        # Standard cancellation (client / rupture_stock): mark annulee + reason
        order.status = Order.ANNULEE
        order.cancel_reason = cancel_reason
        order.cancelled_at = timezone.now()
        order.save(update_fields=["status", "cancel_reason", "cancelled_at", "updated_at"])
        log_action(
            request.user, AuditLog.STATUS_CHANGE,
            description=(
                f"Commande #{order.id} annulée : "
                f"{dict(Order.CANCEL_REASON_CHOICES).get(cancel_reason, cancel_reason)}"
                + (" (bordereau Navex également supprimé)" if navex_was_cancelled else "")
            ),
            request=request,
            target_model="Order", target_id=order.id,
        )
        return JsonResponse({
            "status": "ok", "new_status": Order.ANNULEE,
            "label": valid[Order.ANNULEE],
            "cancel_reason": cancel_reason,
            "navex_was_cancelled": navex_was_cancelled,
        })

    # ---- Other simple transitions (injoignable, pas_serieux, rappeler_plus_tard) ----
    # Valid transitions table
    allowed_transitions = {
        Order.NON_CONFIRMEE: [Order.INJOIGNABLE, Order.PAS_SERIEUX, Order.RAPPELER, Order.ANNULEE],
        Order.RAPPELER:      [Order.INJOIGNABLE, Order.PAS_SERIEUX, Order.ANNULEE],
        Order.INJOIGNABLE:   [Order.RAPPELER, Order.PAS_SERIEUX, Order.ANNULEE],
        Order.PAS_SERIEUX:   [Order.ANNULEE],
        Order.CONFIRMEE:     [Order.ANNULEE],   # only via cancellation flow above
        Order.ANNULEE:       [],                # frozen (admin only)
    }
    if new_status not in allowed_transitions.get(old_status, []):
        return JsonResponse({
            "status": "error",
            "message": f"Transition non permise : {old_label} → {valid[new_status]}.",
            "code": "INVALID_TRANSITION",
        }, status=400)

    order.status = new_status
    order.save(update_fields=["status", "updated_at"])
    log_action(
        request.user, AuditLog.STATUS_CHANGE,
        description=f"Commande #{order.id} : {old_label} → {valid[new_status]}",
        request=request,
        target_model="Order", target_id=order.id,
    )
    return JsonResponse({"status": "ok", "new_status": new_status, "label": valid[new_status]})


def _push_order_to_navex_internal(request, order):
    """Internal: push an order to Navex and return JsonResponse with the result.
    Same logic as api_push_order_to_navex but takes an already-loaded order."""
    import urllib.request, urllib.parse
    from .models import Order, log_action, AuditLog

    token = os.environ.get("NAVEX_API_TOKEN", "")
    if not token:
        return JsonResponse({"status": "error", "message": "NAVEX_API_TOKEN non configuré côté serveur."}, status=500)

    if order.bordereau_barcode:
        return JsonResponse({
            "status": "error",
            "message": f"Déjà envoyé à Navex (bordereau {order.bordereau_barcode}).",
        }, status=400)
    if not order.customer.phone:
        return JsonResponse({"status": "error", "message": "Téléphone manquant."}, status=400)
    if not order.region:
        return JsonResponse({"status": "error", "message": "Gouvernorat manquant."}, status=400)

    designation = _build_designation(order)
    nb_article = _count_articles(order)
    payload = {
        "prix":           f"{order.total:.0f}" if order.total else "0",
        "nom":            order.customer.name or order.customer.phone,
        "gouvernerat":    order.region.name,
        "ville":          order.ville or "",
        "adresse":        (order.address or order.localite or "").strip() or order.ville or "",
        "tel":            order.customer.phone,
        "tel2":           "",
        "designation":    designation[:500],
        "nb_article":     str(nb_article),
        "msg":            (order.notes or "")[:500],
        "echange":        "", "article": "", "nb_echange": "", "ouvrir": "",
        "sender_name":    "", "sender_location": "",
    }
    url = f"https://app.navex.tn/api/rashop-{token}/v1/post.php"
    try:
        body = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                navex_data = json.loads(raw)
            except json.JSONDecodeError:
                navex_data = {"_raw": raw}
    except Exception as e:
        log_action(
            request.user, AuditLog.NAVEX_PUSH,
            description=f"Push #{order.id} ÉCHEC réseau : {str(e)[:200]}",
            request=request, target_model="Order", target_id=order.id,
        )
        return JsonResponse({"status": "error", "message": f"Erreur réseau Navex : {e}"}, status=502)

    if not _navex_response_is_success(navex_data):
        log_action(
            request.user, AuditLog.NAVEX_PUSH,
            description=f"Push #{order.id} REFUSÉ par Navex : {str(navex_data)[:300]}",
            request=request, target_model="Order", target_id=order.id,
            extra=str(navex_data)[:5000],
        )
        return JsonResponse({
            "status": "error",
            "message": navex_data.get("status_message") or navex_data.get("message") or "Navex a refusé la commande.",
            "navex_response": navex_data,
        }, status=400)

    bordereau = _extract_bordereau_from_navex_response(navex_data)
    label_url = navex_data.get("lien") or ""
    order.pushed_to_navex_at = timezone.now()
    if bordereau:
        order.bordereau_barcode = bordereau
    if label_url:
        order.navex_label_url = label_url[:500]
    order.status = Order.CONFIRMEE
    order.save(update_fields=["bordereau_barcode", "navex_label_url", "pushed_to_navex_at", "status", "updated_at"])

    log_action(
        request.user, AuditLog.NAVEX_PUSH,
        description=f"Commande #{order.id} envoyée à Navex" + (f" — bordereau {bordereau}" if bordereau else " (bordereau manquant)"),
        request=request, target_model="Order", target_id=order.id,
        extra=str(navex_data)[:5000],
    )
    return JsonResponse({
        "status": "ok",
        "bordereau": bordereau,
        "label_url": label_url,
        "new_status": Order.CONFIRMEE,
        "label": "Confirmée",
        "redirect": f"/sales-orders/{order.id}/",
    })


# ---------------------------------------------------------------------------
# PHASE 5 — PUSH ORDER TO NAVEX
# Sends a confirmed order to Navex for shipping. On success Navex returns a
# bordereau code which we save back to order.bordereau_barcode.
# ---------------------------------------------------------------------------

def _build_designation(order):
    """Plain-text description of articles for Navex's 'designation' field."""
    parts = []
    for oo in order.order_offers.all():
        offer_parts = []
        for line in oo.lines.all():
            seg = line.product.name
            if line.variant:
                seg += f" {line.variant.color_label or line.variant.color_name}"
            if line.size:
                seg += f" {line.size}"
            if line.quantity > 1:
                seg = f"{line.quantity}× {seg}"
            offer_parts.append(seg)
        offer_label = oo.offer_name or "Offre"
        if oo.quantity > 1:
            offer_label = f"{oo.quantity}× {offer_label}"
        if offer_parts:
            parts.append(f"{offer_label} ({', '.join(offer_parts)})")
        else:
            parts.append(offer_label)
    for line in order.lines.filter(order_offer__isnull=True):
        seg = line.product.name
        if line.variant:
            seg += f" {line.variant.color_label or line.variant.color_name}"
        if line.size:
            seg += f" {line.size}"
        if line.quantity > 1:
            seg = f"{line.quantity}× {seg}"
        parts.append(seg)
    return " | ".join(parts) if parts else "Commande"


def _check_order_stock_rupture(order):
    """For each line in the order, check if the (variant, size) is actually
    in stock. Returns (is_rupture, details_list) where:
        is_rupture = True if at least one line has 0 in_stock+returned units
        details_list = list of {product, variant, size, in_stock_count}
    Used to verify a 'rupture de stock' cancellation reason.
    """
    details = []
    has_rupture = False
    for line in order.lines.all():
        variant = line.variant
        size = line.size
        if not variant or not size:
            continue
        # Count units in_stock or returned (sellable) at this variant+size
        count = variant.units.filter(
            status__in=[ProductUnit.IN_STOCK, ProductUnit.RETURNED],
            size=size,
        ).count()
        details.append({
            "product": line.product.name,
            "variant": variant.color_label or variant.color_name,
            "size": size,
            "needed": line.quantity,
            "in_stock": count,
            "is_short": count < line.quantity,
        })
        if count < line.quantity:
            has_rupture = True
    return has_rupture, details
    """Plain-text description of articles for Navex's 'designation' field."""
    parts = []
    for oo in order.order_offers.all():
        # Show the offer + its products (with chosen variant/size if any)
        offer_parts = []
        for line in oo.lines.all():
            seg = line.product.name
            if line.variant:
                seg += f" {line.variant.color_label or line.variant.color_name}"
            if line.size:
                seg += f" {line.size}"
            if line.quantity > 1:
                seg = f"{line.quantity}× {seg}"
            offer_parts.append(seg)
        offer_label = oo.offer_name or "Offre"
        if oo.quantity > 1:
            offer_label = f"{oo.quantity}× {offer_label}"
        if offer_parts:
            parts.append(f"{offer_label} ({', '.join(offer_parts)})")
        else:
            parts.append(offer_label)
    # Standalone lines (no offer parent)
    for line in order.lines.filter(order_offer__isnull=True):
        seg = line.product.name
        if line.variant:
            seg += f" {line.variant.color_label or line.variant.color_name}"
        if line.size:
            seg += f" {line.size}"
        if line.quantity > 1:
            seg = f"{line.quantity}× {seg}"
        parts.append(seg)
    return " | ".join(parts) if parts else "Commande"


def _count_articles(order):
    """Total number of physical articles for nb_article."""
    n = 0
    for oo in order.order_offers.all():
        for line in oo.lines.all():
            n += line.quantity
        if not oo.lines.exists():
            n += oo.quantity  # offer with no detailed lines counts as itself
    for line in order.lines.filter(order_offer__isnull=True):
        n += line.quantity
    return max(n, 1)


def _extract_bordereau_from_navex_response(resp_data):
    """Find the bordereau code in Navex's response.

    Real Navex success response (verified 2026-05-08):
        {'status': 1, 'lien': '...', 'status_message': '918762425951'}

    The bordereau is the digit string in status_message.
    Fallback: look for known field names in case Navex changes the format.
    """
    if not isinstance(resp_data, dict):
        return ""
    # Real-world success: status_message is the bordereau (a digit string)
    sm = resp_data.get("status_message", "")
    if isinstance(sm, (str, int)):
        s = str(sm).strip()
        if s.isdigit() and len(s) >= 6:  # looks like a bordereau number
            return s
    # Fallback: try other plausible keys
    for key in ("code", "barcode", "bordereau", "bordereau_barcode", "tracking", "id"):
        v = resp_data.get(key)
        if v and isinstance(v, (str, int)):
            return str(v).strip()
    colis = resp_data.get("colis")
    if isinstance(colis, dict):
        for key in ("code", "barcode", "bordereau", "bordereau_barcode", "tracking", "id"):
            v = colis.get(key)
            if v and isinstance(v, (str, int)):
                return str(v).strip()
    return ""


def _navex_response_is_success(resp_data):
    """Detect a successful Navex create response.

    Real Navex success: status == 1 (integer) and status_message contains a digit string.
    Failure: status_message contains 'ERREUR' or status is something else.
    """
    if not isinstance(resp_data, dict):
        return False
    status = resp_data.get("status")
    if status == 1 or status == "1" or status == "ok" or status == "success":
        return True
    # Fallback: success if we can extract a bordereau-looking number anywhere
    bc = _extract_bordereau_from_navex_response(resp_data)
    if bc and bc.isdigit() and len(bc) >= 6:
        return True
    return False


def _navex_fetch_one(bordereau):
    """Fetch the current Navex status for a single bordereau.
    Returns (ok: bool, parsed: dict, raw_response: dict|str).
    The parsed dict has keys: etat, motif, pre_etat, livreur, livreur_tel.
    Uses the bulk endpoint with codes= for consistency.
    """
    ok, items, raw = _navex_fetch_many([bordereau])
    if not ok or not items:
        return False, {}, raw
    return True, items.get(bordereau, {}), raw


def _navex_fetch_many(bordereaux):
    """Fetch Navex status for a list of bordereaux in ONE API call.
    Returns (ok: bool, items_by_code: dict, raw_response: dict|str).

    items_by_code maps {code: {"etat": "...", "motif": "...", "pre_etat": "...",
                               "livreur": "...", "livreur_tel": "...",
                               "found": bool}}.
    """
    import urllib.request, urllib.parse
    token = os.environ.get("NAVEX_API_TOKEN", "")
    if not token or not bordereaux:
        return False, {}, {"_error": "missing token or bordereaux"}

    # Use the etat (read) endpoint with codes= (plural) per Navex docs.
    url = f"https://app.navex.tn/api/rashop-etat-{token}/v1/post.php"
    codes_string = ", ".join(b for b in bordereaux if b)
    try:
        body = urllib.parse.urlencode({"codes": codes_string}).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                return False, {}, {"_raw": raw}
    except Exception as e:
        return False, {}, {"_error": f"network: {e}"}

    items = {}
    if isinstance(data, dict):
        results = data.get("results") or []
        if isinstance(results, list):
            for entry in results:
                if not isinstance(entry, dict):
                    continue
                code = str(entry.get("code", "")).strip()
                if not code:
                    continue
                items[code] = {
                    "etat":        str(entry.get("etat") or "").strip(),
                    "motif":       str(entry.get("motif") or "").strip(),
                    "pre_etat":    str(entry.get("pre_etat") or "").strip(),
                    "livreur":     str(entry.get("livreur") or "").strip(),
                    "livreur_tel": str(entry.get("livreur_tel") or "").strip(),
                    "found":       bool(entry.get("status") == 1),
                }
    return True, items, data


def _sync_navex_status_for_order(order, force=False):
    """Refresh navex status fields for a single Order. Returns True if updated."""
    if not order.bordereau_barcode:
        return False
    ok, parsed, raw = _navex_fetch_one(order.bordereau_barcode)
    if not ok or not parsed:
        return False
    order.navex_last_status   = (parsed.get("etat") or "")[:80]
    order.navex_motif         = (parsed.get("motif") or "")[:200]
    order.navex_pre_etat      = (parsed.get("pre_etat") or "")[:80]
    order.navex_livreur       = (parsed.get("livreur") or "")[:120]
    order.navex_livreur_tel   = (parsed.get("livreur_tel") or "")[:30]
    order.navex_last_status_raw = str(raw)[:5000]
    order.navex_last_synced_at = timezone.now()
    order.save(update_fields=[
        "navex_last_status", "navex_motif", "navex_pre_etat",
        "navex_livreur", "navex_livreur_tel",
        "navex_last_status_raw", "navex_last_synced_at", "updated_at",
    ])
    return True


def _sync_navex_for_v2_orders(only_pending=True):
    """Bulk-refresh Navex status for v2 orders that have a bordereau.

    Uses Navex's bulk endpoint: ALL orders synced in ONE API call.
    only_pending=True (default): skip orders already in Annulée status.
    Returns (n_attempted, n_updated).
    """
    from .models import Order
    qs = Order.objects.exclude(bordereau_barcode="")
    if only_pending:
        qs = qs.exclude(status=Order.ANNULEE)
    orders = list(qs)
    n_attempted = len(orders)
    if n_attempted == 0:
        return 0, 0

    # Bulk fetch
    bordereaux = [o.bordereau_barcode for o in orders]
    ok, items, raw = _navex_fetch_many(bordereaux)
    if not ok:
        return n_attempted, 0

    n_updated = 0
    now = timezone.now()
    raw_str = str(raw)[:5000]
    for o in orders:
        parsed = items.get(o.bordereau_barcode)
        if not parsed:
            continue
        o.navex_last_status   = (parsed.get("etat") or "")[:80]
        o.navex_motif         = (parsed.get("motif") or "")[:200]
        o.navex_pre_etat      = (parsed.get("pre_etat") or "")[:80]
        o.navex_livreur       = (parsed.get("livreur") or "")[:120]
        o.navex_livreur_tel   = (parsed.get("livreur_tel") or "")[:30]
        o.navex_last_status_raw = raw_str
        o.navex_last_synced_at = now
        o.save(update_fields=[
            "navex_last_status", "navex_motif", "navex_pre_etat",
            "navex_livreur", "navex_livreur_tel",
            "navex_last_status_raw", "navex_last_synced_at", "updated_at",
        ])
        n_updated += 1
    return n_attempted, n_updated


def _navex_cancel_colis(bordereau):
    """Call Navex's delete endpoint to cancel an existing colis.

    Returns (ok: bool, response_data: dict|str).
    On any error (network, refused), ok=False.
    Endpoint:
        POST https://app.navex.tn/api/rashop-delete-{TOKEN}/v1/post.php
        body: delete_code=<bordereau>
    """
    import urllib.request, urllib.parse
    token = os.environ.get("NAVEX_API_TOKEN", "")
    if not token:
        return False, {"_error": "NAVEX_API_TOKEN non configuré côté serveur."}
    if not bordereau:
        return False, {"_error": "Bordereau vide."}

    url = f"https://app.navex.tn/api/rashop-delete-{token}/v1/post.php"
    try:
        body = urllib.parse.urlencode({"delete_code": bordereau}).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {"_raw": raw}
    except Exception as e:
        return False, {"_error": f"Erreur réseau: {e}"}

    # Success: status==1 OR status_message looks positive (no "ERREUR")
    if isinstance(data, dict):
        status = data.get("status")
        msg = str(data.get("status_message") or data.get("message") or "")
        if status == 1 or status == "1" or status == "ok" or status == "success":
            return True, data
        if msg and "erreur" not in msg.lower() and "error" not in msg.lower():
            # Likely success even without explicit status==1
            return True, data
    return False, data


@csrf_exempt
@require_POST
@login_required(login_url="/login/")
def api_push_order_to_navex(request, pk):
    """Manual fallback: push a v2 Order to Navex.
    The recommended path is now via the 'Confirmée' status button which calls
    api_order_change_status — this endpoint stays as a manual fallback button.
    """
    from .models import Order
    if not _orders_role_check(request):
        return JsonResponse({"status": "error", "message": "Accès refusé."}, status=403)
    try:
        order = Order.objects.select_related("customer", "region", "sales_page").prefetch_related(
            "order_offers__lines__product", "order_offers__lines__variant",
            "lines__product", "lines__variant",
        ).get(pk=pk)
    except Order.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Commande introuvable."}, status=404)
    return _push_order_to_navex_internal(request, order)


@csrf_exempt
@require_POST
@login_required(login_url="/login/")
def api_sync_v2_orders_navex(request):
    """Manual button: sync all pending v2 orders' Navex status now."""
    if not _orders_role_check(request):
        return JsonResponse({"status": "error", "message": "Accès refusé."}, status=403)
    from .models import log_action, AuditLog
    n_attempted, n_updated = _sync_navex_for_v2_orders(only_pending=True)
    log_action(
        request.user, AuditLog.NAVEX_SYNC,
        description=f"Sync Navex v2: {n_updated}/{n_attempted} commandes mises à jour",
        request=request,
    )
    return JsonResponse({
        "status": "ok",
        "attempted": n_attempted,
        "updated": n_updated,
    })


@login_required(login_url="/login/")
def unit_detail(request, barcode):
    """Show the full history of a single ProductUnit (barcode)."""
    from .models import ProductUnit, StockMovement, OrderItem
    barcode = (barcode or "").strip().upper()
    try:
        unit = ProductUnit.objects.select_related("variant__product").get(barcode=barcode)
    except ProductUnit.DoesNotExist:
        return render(request, "inventory/unit_not_found.html", {"barcode": barcode}, status=404)

    movements = StockMovement.objects.filter(unit=unit).select_related("user").order_by("-moved_at")
    # Find all orders this unit appeared in (for context)
    order_items = OrderItem.objects.filter(unit=unit).select_related("order").order_by("-scanned_at")

    return render(request, "inventory/unit_detail.html", {
        "unit": unit,
        "movements": movements,
        "order_items": order_items,
    })
