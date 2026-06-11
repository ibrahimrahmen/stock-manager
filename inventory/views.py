import json
import os
from decimal import Decimal
from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse, HttpResponseForbidden, HttpResponse
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


def get_scan_session_date():
    """Return the date that scans should be grouped under.

    Normal days: today's date.
    Sunday (weekday=6): returns Saturday's date — the shipping company doesn't
    pick up packages on Saturday, only on Sunday. So Saturday + Sunday scans
    are fused into a single session under the Saturday date. The session
    only resets on Monday.
    """
    today = timezone.now().date()
    if today.weekday() == 6:  # Sunday
        # Roll back to Saturday (1 day earlier)
        from datetime import timedelta
        return today - timedelta(days=1)
    return today


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


# ---- Internal sale (employee / friend) -------------------------------------

@login_required(login_url="/login/")
def internal_sale_view(request):
    """Page where admin scans a unit barcode and sells it at cost price (or custom)."""
    if not request.user.is_staff:
        return HttpResponseForbidden("Accès réservé aux administrateurs.")
    return render(request, "inventory/internal_sale.html", {})


@csrf_exempt
@require_POST
@login_required(login_url="/login/")
def api_internal_sale_lookup(request):
    """Look up a ProductUnit by barcode. Returns its info + prices."""
    if not request.user.is_staff:
        return JsonResponse({"status": "error", "message": "Permission refusée."}, status=403)
    data = json.loads(request.body)
    barcode = (data.get("barcode") or "").strip().upper()
    if not barcode:
        return JsonResponse({"status": "error", "message": "Code-barres vide."})
    try:
        unit = ProductUnit.objects.select_related("variant", "variant__product").get(barcode=barcode)
    except ProductUnit.DoesNotExist:
        return JsonResponse({"status": "error", "message": f"Unité {barcode} introuvable."})

    product = unit.variant.product
    return JsonResponse({
        "status": "ok",
        "unit": {
            "barcode": unit.barcode,
            "status_raw": unit.status,
            "product_name": product.name,
            "color_label": unit.variant.color_label or "",
            "size": unit.size or "",
            "buy_price": str(product.buy_price),
            "sell_price": str(product.sell_price),
        }
    })


@csrf_exempt
@require_POST
@login_required(login_url="/login/")
def api_internal_sale_confirm(request):
    """Mark a unit as PAID at a custom price. Creates StockMovement + AuditLog."""
    if not request.user.is_staff:
        return JsonResponse({"status": "error", "message": "Permission refusée."}, status=403)
    from .models import log_action, AuditLog
    data = json.loads(request.body)
    barcode = (data.get("barcode") or "").strip().upper()
    sale_price_raw = (data.get("sale_price") or "").strip()
    note = (data.get("note") or "").strip()

    if not barcode:
        return JsonResponse({"status": "error", "message": "Code-barres vide."})
    try:
        sale_price = Decimal(sale_price_raw)
    except Exception:
        return JsonResponse({"status": "error", "message": "Prix invalide."})

    try:
        unit = ProductUnit.objects.select_related("variant", "variant__product").get(barcode=barcode)
    except ProductUnit.DoesNotExist:
        return JsonResponse({"status": "error", "message": f"Unité {barcode} introuvable."})

    if unit.status == ProductUnit.PAID:
        return JsonResponse({"status": "error", "message": f"Unité {barcode} déjà vendue."})
    if unit.status == ProductUnit.SHIPPED:
        return JsonResponse({"status": "error", "message": f"Unité {barcode} déjà expédiée — utilise le scan paiement normal."})

    old_status = unit.status
    unit.status = ProductUnit.PAID
    unit.save(update_fields=["status"])

    StockMovement.objects.create(
        unit=unit,
        movement_type=StockMovement.PAID,
        reference=f"VENTE INTERNE - {note}" if note else "VENTE INTERNE",
        user=request.user,
    )

    log_action(
        request.user, AuditLog.OTHER,
        description=(
            f"Vente interne : {unit.variant.product.name} ({unit.variant.color_label or '?'}, "
            f"taille {unit.size or '?'}) vendu à {sale_price} DT"
            + (f" — Note: {note}" if note else "")
            + f" (ancien statut: {old_status})"
        ),
        request=request,
        target_unit_barcode=barcode,
    )

    return JsonResponse({
        "status": "ok",
        "message": f"Unité {barcode} vendue à {sale_price} DT.",
        "product_name": unit.variant.product.name,
        "color": unit.variant.color_label or "",
        "size": unit.size or "",
    })


@login_required(login_url="/login/")
def stock_value(request):
    if not request.user.is_staff:
        return HttpResponseForbidden("Acces reserve aux administrateurs.")

    # Group by PRODUCT (sum across all variants/colors/sizes).
    from collections import defaultdict
    variants = ProductVariant.objects.select_related("product").prefetch_related("units")
    agg = defaultdict(lambda: {
        "product": None,
        "in_stock": 0,
        "shipped": 0,
        "returned": 0,
        "total_units": 0,
        "buy_total": Decimal("0"),
        "sell_total": Decimal("0"),
    })
    total_buy        = Decimal("0")
    total_sell       = Decimal("0")
    total_buy_shipped  = Decimal("0")
    total_sell_shipped = Decimal("0")

    for variant in variants:
        in_stock       = variant.units.filter(status=ProductUnit.IN_STOCK).count()
        shipped        = variant.units.filter(status=ProductUnit.SHIPPED).count()
        pending_return = variant.units.filter(status=ProductUnit.RETURNED).count()
        total_units    = in_stock + shipped + pending_return
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
        # Aggregate into per-product row
        row = agg[variant.product_id]
        row["product"] = variant.product
        row["in_stock"]    += in_stock
        row["shipped"]     += shipped
        row["returned"]    += pending_return
        row["total_units"] += total_units
        row["buy_total"]   += buy
        row["sell_total"]  += sell
        # Pick the first variant image we find — that's our product thumbnail
        if not row.get("image") and variant.image:
            row["image"] = variant.image
            row["image_color"] = variant.color_name or ""

    rows = sorted(agg.values(), key=lambda r: r["product"].name.lower())
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



def _do_return_unit(unit, user=None):
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
    StockMovement.objects.create(unit=unit, movement_type=StockMovement.RETURNED, reference="RETOUR", user=user)
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

    # Propagate to the linked v2 Order: if this v1 ShippingOrder is now
    # RETURNED or PARTIAL_RETURNED and is linked to a v2 Order, move that v2
    # Order to RETURNED ("Retourné", final). It then drops out of the active
    # lists and the Navex sync.
    if order.status in (ShippingOrder.RETURNED, ShippingOrder.PARTIAL_RETURNED):
        try:
            from .models import Order as _V2Order, log_action, AuditLog
            v2 = getattr(order, "order", None)
            if v2 is not None and v2.status != _V2Order.RETURNED:
                old_label = dict(_V2Order.STATUS_CHOICES).get(v2.status, v2.status)
                v2.status = _V2Order.RETURNED
                v2.save(update_fields=["status", "updated_at"])
                log_action(
                    None, AuditLog.STATUS_CHANGE,
                    description=f"Auto: commande v2 #{v2.id} {old_label} → 'Retourné' "
                                f"(scan retour v1, bordereau {order.bordereau_barcode})",
                    target_model="Order", target_id=v2.id,
                )
        except Exception:
            # Never let v2 propagation break the v1 return flow.
            pass


@csrf_exempt
@require_POST
def api_scan_return(request):
    from .barcode_parser import is_bordereau_barcode
    from .models import log_action, AuditLog, Order, ExchangeReturnItem
    data = json.loads(request.body)
    barcode = data.get("barcode", "").strip().upper()
    if not barcode:
        return JsonResponse({"status": "error", "message": "Barcode vide."}, status=400)

    # CASE 1: Check if this is an exchange return code (code_echange from Navex)
    # An exchange's return colis has a different barcode from the original push
    # bordereau. It's stored in Order.navex_return_barcode when Navex assigns it.
    # We check this FIRST, before the regular ShippingOrder lookup.
    try:
        exchange_order = Order.objects.prefetch_related(
            "return_items__variant__product"
        ).get(navex_return_barcode=barcode)
    except Order.DoesNotExist:
        exchange_order = None
    except Order.MultipleObjectsReturned:
        # Defensive: if somehow two orders share the same return barcode, take the most recent
        exchange_order = Order.objects.filter(navex_return_barcode=barcode).order_by("-created_at").first()

    if exchange_order is not None:
        # Build the items list from ExchangeReturnItem (the products the customer
        # said they would return when the exchange was created).
        return_items = exchange_order.return_items.all()
        if not return_items.exists():
            log_action(
                request.user, AuditLog.SCAN_RETURN,
                description=f"Retour échange : code {barcode} (commande #{exchange_order.id}) mais aucun article à retourner enregistré",
                request=request, target_order_barcode=barcode,
            )
            return JsonResponse({
                "status": "error",
                "message": f"Échange #{exchange_order.id} reconnu, mais aucun article à retourner n'a été enregistré.",
                "code": "EXCHANGE_NO_RETURNS",
            })

        # Format items in the same shape as the v1 scan_return response.
        # We don't have a physical ProductUnit here (the scan of the unit will
        # come later — at this stage we just show what's EXPECTED).
        items_data = []
        for ri in return_items:
            variant = ri.variant
            product = variant.product if variant else None
            items_data.append({
                # No physical barcode yet — use the ExchangeReturnItem ID as placeholder
                "barcode": f"RETURN-{ri.id}",
                "size": ri.size,
                "status": "pending_return",  # Custom status: still waiting for scan
                "product_name": product.name if product else (ri.product_name_snapshot or "?"),
                "color_label": variant.color_label if variant else "",
                "sell_price": str(product.sell_price) if product else "0",
                "image_url": variant.image.url if variant and variant.image else None,
                "exchange_return_item_id": ri.id,
            })

        log_action(
            request.user, AuditLog.SCAN_RETURN,
            description=f"Code d'échange retour scanné : {barcode} → commande échange #{exchange_order.id} ({len(items_data)} article(s) à recevoir)",
            request=request, target_order_barcode=barcode,
        )
        return JsonResponse({
            "status": "ok",
            "type": "order_multiple",
            "order_bordereau": barcode,
            "order_id": exchange_order.id,
            "is_exchange": True,
            "exchange_of_id": exchange_order.exchange_of_id,
            "items": items_data,
        })

    # CASE 2: Regular v1 ShippingOrder lookup (existing logic, unchanged)
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
        RETURNABLE_STATUSES = {"shipped", "paid", "expédié", "expedie",
                               "early_return", "at_depot"}
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
                    "early_return": "Retour anticipé",
                    "at_depot": "Retour en dépôt Navex",
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
        # Always open the modal regardless of unit count.
        # The "auto-return on single unit" shortcut was a footgun: a single
        # accidental scan would mark an item returned with no confirmation.
        # Now the worker must scan the actual product barcode inside the modal,
        # which guarantees they have the physical item in hand.
        log_action(
            request.user, AuditLog.SCAN_RETURN,
            description=f"Retour ouvert pour {barcode} ({len(returnable)} retournable(s))",
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
    if unit.status not in (ProductUnit.SHIPPED, ProductUnit.PAID,
                           ProductUnit.EARLY_RETURN, ProductUnit.AT_DEPOT):
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
    unit_data, reconciliation = _do_return_unit(unit, user=_user_for_request(request))
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
def api_exchange_mark_received(request):
    """Phase 2.5b: mark exchange return item(s) as RECEIVED when the return
    colis from an exchange is scanned/confirmed.

    Body (either form):
      {"exchange_return_item_id": 12}                 # mark one item received
      {"exchange_return_item_ids": [12, 13]}          # mark several
      {"missing_ids": [14]}                           # optionally flag missing

    For each item marked received: if it is linked to a physical ProductUnit,
    that unit is returned to stock (status IN_STOCK + a StockMovement), mirroring
    the normal return flow.
    """
    from .models import log_action, AuditLog, ExchangeReturnItem, ProductUnit, StockMovement
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "JSON invalide."}, status=400)

    ids = data.get("exchange_return_item_ids")
    if not ids:
        single = data.get("exchange_return_item_id")
        ids = [single] if single else []
    missing_ids = data.get("missing_ids") or []

    if not ids and not missing_ids:
        return JsonResponse({"status": "error", "message": "Aucun article à marquer."}, status=400)

    received_count = 0
    restocked = 0
    with transaction.atomic():
        # Mark received
        for ri in ExchangeReturnItem.objects.select_related("unit").filter(id__in=[int(i) for i in ids]):
            if ri.status != ExchangeReturnItem.RECEIVED_OK:
                ri.status = ExchangeReturnItem.RECEIVED_OK
                ri.save(update_fields=["status", "updated_at"])
                received_count += 1
                # If we know the physical unit, mark it returned (back from the
                # customer, sellable but flagged as a return for visibility).
                if ri.unit_id:
                    unit = ri.unit
                    if unit.status != ProductUnit.RETURNED:
                        unit.status = ProductUnit.RETURNED
                        unit.save(update_fields=["status"])
                        StockMovement.objects.create(
                            unit=unit, movement_type=StockMovement.RECEIVED,
                            reference=f"RETOUR ÉCHANGE #{ri.exchange_order_id}",
                            user=_user_for_request(request),
                        )
                        restocked += 1
        # Mark missing
        marked_missing = 0
        for ri in ExchangeReturnItem.objects.filter(id__in=[int(i) for i in missing_ids]):
            if ri.status != ExchangeReturnItem.RECEIVED_MISSING:
                ri.status = ExchangeReturnItem.RECEIVED_MISSING
                ri.save(update_fields=["status", "updated_at"])
                marked_missing += 1

    log_action(
        request.user, AuditLog.SCAN_RETURN,
        description=f"Retour échange : {received_count} article(s) reçu(s), {restocked} remis en stock"
                    + (f", {marked_missing} manquant(s)" if missing_ids else ""),
        request=request,
    )
    return JsonResponse({
        "status": "ok",
        "received": received_count,
        "restocked": restocked,
        "missing": len(missing_ids),
    })


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
            if unit.status in (ProductUnit.SHIPPED, ProductUnit.PAID,
                               ProductUnit.EARLY_RETURN, ProductUnit.AT_DEPOT):
                unit_data, reconciliation = _do_return_unit(unit, user=_user_for_request(request))
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

    # If linked to a Shopify-originating v2 Order, mark it paid on Shopify too.
    try:
        v2 = order.order
        if v2 and v2.notes:
            shopify_id = _extract_shopify_order_id_from_notes(v2.notes)
            if shopify_id:
                ok_sh, sh_resp = _shopify_mark_paid(shopify_id, amount_collected, "TND")
                if ok_sh:
                    log_action(
                        request.user, AuditLog.PAYMENT,
                        description=f"Shopify : commande {shopify_id} marquée payée ({amount_collected} TND, Order v2 #{v2.id})",
                        request=request,
                        target_model="Order", target_id=v2.id,
                    )
                else:
                    err_msg = sh_resp.get("_error") if isinstance(sh_resp, dict) else str(sh_resp)
                    log_action(
                        request.user, AuditLog.PAYMENT,
                        description=f"Shopify : ÉCHEC mark paid pour {shopify_id} (Order v2 #{v2.id}) — {err_msg}",
                        request=request,
                        target_model="Order", target_id=v2.id,
                    )
    except Exception:
        pass

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
        # Mirror the collected amount onto the linked v2 Order (if any) so the
        # v2 list reflects what was actually collected. Only the collected
        # amount is synced — the computed `total` is left untouched. Also flip
        # the v2 status to PAYEE ("Payée") — but never override a return
        # (RETURNED stays final).
        v2 = getattr(order, "order", None)
        if v2 is not None:
            from .models import Order as _V2Order
            v2.amount_collected = amount
            fields = ["amount_collected", "updated_at"]
            if v2.status != _V2Order.RETURNED and v2.status != _V2Order.PAYEE:
                v2.status = _V2Order.PAYEE
                fields.append("status")
            v2.save(update_fields=fields)

    log_action(
        request.user, AuditLog.PAYMENT,
        description=f"Ordre {order.bordereau_barcode} marqué payé via Navex — {amount} TND",
        request=request,
        target_order_barcode=order.bordereau_barcode,
        target_model="ShippingOrder", target_id=order.id,
    )

    # If this ShippingOrder is linked to a v2 Order that came from Shopify,
    # mirror the PAID status back to Shopify (creates a successful transaction
    # so the merchant dashboard shows the order as Paid instead of Pending).
    try:
        v2 = order.order
        if v2 and v2.notes:
            shopify_id = _extract_shopify_order_id_from_notes(v2.notes)
            if shopify_id:
                ok_sh, sh_resp = _shopify_mark_paid(shopify_id, amount, "TND")
                if ok_sh:
                    log_action(
                        request.user, AuditLog.PAYMENT,
                        description=f"Shopify : commande {shopify_id} marquée payée ({amount} TND, Order v2 #{v2.id})",
                        request=request,
                        target_model="Order", target_id=v2.id,
                    )
                else:
                    err_msg = sh_resp.get("_error") if isinstance(sh_resp, dict) else str(sh_resp)
                    log_action(
                        request.user, AuditLog.PAYMENT,
                        description=f"Shopify : ÉCHEC mark paid pour {shopify_id} (Order v2 #{v2.id}) — {err_msg}",
                        request=request,
                        target_model="Order", target_id=v2.id,
                    )
    except Exception as _e:
        # Never let Shopify sync break our local payment confirmation
        pass

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


def api_check_duplicate_client(request):
    """Check whether a phone number has other ShippingOrders still in the
    delivery cycle (OPEN or CLOSED — i.e. not yet PAID/RETURNED).

    Used to warn the warehouse team when they're about to ship a SECOND
    parcel to a client who already has an in-flight first parcel — common
    pattern of duplicate orders across multiple sales pages.

    GET ?phone=12345678&exclude_id=NNN
    """
    phone_raw = (request.GET.get("phone") or "").strip()
    exclude_id = request.GET.get("exclude_id") or ""

    if not phone_raw:
        return JsonResponse({"status": "ok", "duplicates": []})

    # Normalize: keep digits only (handles "+216 12-345-678" → "21612345678")
    import re as _re
    digits = _re.sub(r"\D", "", phone_raw)
    if len(digits) < 6:
        return JsonResponse({"status": "ok", "duplicates": []})

    # Match by suffix to tolerate +216 prefix variations.
    # We only return non-finalized orders (still in delivery cycle).
    suffix = digits[-8:]  # last 8 digits = TN phone number
    qs = ShippingOrder.objects.filter(client_phone__icontains=suffix)
    if exclude_id:
        try:
            qs = qs.exclude(pk=int(exclude_id))
        except Exception:
            pass
    # Only orders still in delivery cycle (not paid, not returned, not cancelled)
    qs = qs.exclude(status__in=(
        ShippingOrder.PAID,
        ShippingOrder.PARTIAL_PAID,
        ShippingOrder.RETURNED,
        ShippingOrder.CANCELLED,
    ))
    qs = qs.order_by("-created_at")[:5]

    duplicates = []
    for o in qs:
        duplicates.append({
            "id": o.id,
            "bordereau_barcode": o.bordereau_barcode,
            "client_name": o.client_name,
            "status": o.get_status_display(),
            "created_at": o.created_at.strftime("%d/%m/%Y %H:%M"),
            "designation": o.navex_designation[:120] if o.navex_designation else "",
        })
    return JsonResponse({"status": "ok", "duplicates": duplicates})


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
    
    if unit.status not in (ProductUnit.SHIPPED, ProductUnit.PAID,
                           ProductUnit.EARLY_RETURN, ProductUnit.AT_DEPOT):
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
    today = get_scan_session_date()  # rolls Saturday session over Sunday
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
    """Get today's scan session log — dedupes by bordereau (latest wins).
    Also resolves order_id for each bordereau so the UI can link to detail pages.
    """
    today = get_scan_session_date()  # rolls Saturday session over Sunday
    logs = ScanSessionLog.objects.filter(session_date=today).order_by("-scanned_at")
    seen = set()
    deduped = []
    for log in logs:
        if log.bordereau_barcode in seen:
            continue
        seen.add(log.bordereau_barcode)
        deduped.append(log)

    # Resolve order_id per bordereau in ONE query (avoid N+1)
    bordereaux = [l.bordereau_barcode for l in deduped]
    order_ids = dict(
        ShippingOrder.objects.filter(bordereau_barcode__in=bordereaux)
        .values_list("bordereau_barcode", "id")
    )

    def serialize(l, is_correct):
        d = {
            "bc": l.bordereau_barcode,
            "designation": l.designation,
            "units": l.unit_count,
            "time": l.scanned_at.strftime("%H:%M"),
            "order_id": order_ids.get(l.bordereau_barcode),
        }
        if not is_correct:
            d["reason"] = l.reason
        return d

    return JsonResponse({
        "status": "ok",
        "correct": [serialize(l, True) for l in deduped if l.is_correct],
        "wrong": [serialize(l, False) for l in deduped if not l.is_correct],
    })


@csrf_exempt
@require_POST
@login_required(login_url="/login/")
def api_clear_scan_session_today(request):
    """Delete all ScanSessionLog rows for today (the "Vider" button).

    Does NOT touch ShippingOrders, ProductUnits, or any real shipping data —
    only clears the display log so the warehouse team starts fresh after a
    big pickup or a holiday batch.

    Requires staff permission to avoid accidental clicks.
    """
    if not request.user.is_staff:
        return JsonResponse({"status": "error", "message": "Permission refusée."}, status=403)

    today = get_scan_session_date()
    qs = ScanSessionLog.objects.filter(session_date=today)
    deleted_count = qs.count()
    qs.delete()

    log_action(
        request.user, AuditLog.OTHER,
        description=f"Session scan : vidé manuellement ({deleted_count} entrée(s) supprimée(s) pour {today})",
        request=request,
    )

    return JsonResponse({"status": "ok", "deleted": deleted_count})


@login_required(login_url="/login/")
def api_recheck_session(request):
    """Full reconciliation: walk every order closed today, fetch Navex info,
    rebuild the ScanSessionLog rows. Creates missing rows, updates existing
    ones — re-derives is_correct from Navex designation vs scanned products.
    """
    import urllib.request, urllib.parse
    today = get_scan_session_date()  # rolls Saturday session over Sunday

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

        # Designation vs scanned products: compare with QUANTITIES.
        # Navex sometimes lists the same product multiple times (one per unit
        # ordered). We count how many of each product are expected vs scanned.
        if designation:
            items_in_desig = [part.strip() for part in designation.split(",")]
            if items_in_desig and "|" in items_in_desig[0]:
                items_in_desig[0] = items_in_desig[0].split("|", 1)[1].strip()

            # Count expected: each item line in the designation is one unit
            from collections import Counter
            expected_counter = Counter()
            for item in items_in_desig:
                item_lower = item.lower()
                # Use longest product name first to handle "Polo Ling Hiver" vs "Polo Ling"
                sorted_products = sorted(all_products, key=lambda p: len(p.name), reverse=True)
                for product in sorted_products:
                    if product.name.lower() in item_lower:
                        # Use the parent's name if this is a V2/V3 (so V2 counts as same SKU)
                        canonical_name = (
                            product.parent_product.name.lower()
                            if product.parent_product
                            else product.name.lower()
                        )
                        expected_counter[canonical_name] += 1
                        break

            # Count scanned: walk units we actually scanned. Use canonical name too
            scanned_counter = Counter()
            for n in scanned_names:
                if not n:
                    continue
                # Look up the product to find its parent if any
                canonical = n.lower()
                for p in all_products:
                    if p.name.lower() == n.lower() and p.parent_product:
                        canonical = p.parent_product.name.lower()
                        break
                scanned_counter[canonical] += 1

            # Find missing (expected but not enough scanned) and excess (scanned more than expected)
            missing_parts = []
            excess_parts = []
            for product_name, exp_qty in expected_counter.items():
                scan_qty = scanned_counter.get(product_name, 0)
                if scan_qty < exp_qty:
                    missing_parts.append(f"{product_name} (×{exp_qty - scan_qty})")
            for product_name, scan_qty in scanned_counter.items():
                exp_qty = expected_counter.get(product_name, 0)
                if scan_qty > exp_qty:
                    excess_parts.append(f"{product_name} (+{scan_qty - exp_qty})")

            if missing_parts:
                reasons.append(f"Produits manquants: {', '.join(missing_parts)}")
            if excess_parts:
                reasons.append(f"Produits en trop: {', '.join(excess_parts)}")

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
    today = get_scan_session_date()  # rolls Saturday session over Sunday
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
    from .models import log_action, AuditLog, Order
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
        n_early_returned = 0
        n_returned_confirmed = 0
        for order in orders:
            bc = order["bordereau_barcode"]
            navex = navex_map.get(bc, None)
            navex_etat = navex.get("etat", "Introuvable") if navex and navex.get("status") == 1 else "Introuvable"
            needs_attention = navex_etat in ("Livrer Paye", "Livré", "Livrée", "Livré Payé")
            is_anomaly = navex_etat in ("Retourné", "Retourne", "Annulé", "Annule")

            # Auto-flip ProductUnit statuses for v1 ShippingOrder based on Navex etat.
            # Workflow:
            #   SHIPPED → EARLY_RETURN  (Navex "Rtn client/agence" — customer refused, on way back)
            #   EARLY_RETURN → AT_DEPOT  (Navex "Retour Depot Navex" — arrived at hub, waiting for our pickup)
            #   AT_DEPOT → RETURNED      (we physically scan it back at our warehouse — done elsewhere)
            navex_lower = navex_etat.strip().lower()
            try:
                so = ShippingOrder.objects.get(pk=order["id"])
                if navex_lower in ("rtn client/agence", "rtn client", "rtn agence", "retour anticipe", "retour anticipé"):
                    flipped = 0
                    for item in so.items.select_related("unit"):
                        if item.unit and item.unit.status == ProductUnit.SHIPPED:
                            item.unit.status = ProductUnit.EARLY_RETURN
                            item.unit.save(update_fields=["status", "updated_at"])
                            StockMovement.objects.create(
                                unit=item.unit,
                                movement_type=StockMovement.EARLY_RETURN,
                                reference=f"Auto sync Navex — bordereau {bc}",
                                user=request.user,
                            )
                            flipped += 1
                    if flipped:
                        n_early_returned += flipped
                        log_action(
                            request.user, AuditLog.STATUS_CHANGE,
                            description=f"Auto sync Navex: {flipped} unité(s) SHIPPED → EARLY_RETURN (bordereau {bc}, etat '{navex_etat}')",
                            request=request,
                        )
                elif navex_lower in ("retour recu", "retour reçu", "retourne", "retourné", "retour confirme", "retour confirmé"):
                    # Unit has arrived at Navex hub, waiting for our physical pickup.
                    # Flip SHIPPED and EARLY_RETURN units → AT_DEPOT.
                    flipped = 0
                    for item in so.items.select_related("unit"):
                        if item.unit and item.unit.status in (ProductUnit.SHIPPED, ProductUnit.EARLY_RETURN):
                            item.unit.status = ProductUnit.AT_DEPOT
                            item.unit.save(update_fields=["status", "updated_at"])
                            StockMovement.objects.create(
                                unit=item.unit,
                                movement_type=StockMovement.AT_DEPOT,
                                reference=f"Auto sync Navex — bordereau {bc}",
                                user=request.user,
                            )
                            flipped += 1
                    if flipped:
                        log_action(
                            request.user, AuditLog.STATUS_CHANGE,
                            description=f"Auto sync Navex: {flipped} unité(s) → AT_DEPOT (bordereau {bc}, etat '{navex_etat}')",
                            request=request,
                        )

                # v2 Order status: Navex "Au magasin" / "En cours" / return
                # states → move the linked v2 Order into that status. Forward
                # states only from Confirmée; "En retour" (Navex "Retour
                # Expéditeur" or "Rtn client/agence") from any in-transit state.
                # NOTE: "Retourné" (final) is NOT set here — it is set when the
                # return is physically scanned in v1.
                linked_order = getattr(so, "order", None)
                if linked_order:
                    new_v2_status = None
                    if linked_order.status == Order.CONFIRMEE:
                        if navex_lower in ("au magasin", "au-magasin", "au magasin navex"):
                            new_v2_status = Order.AU_MAGASIN
                        elif navex_lower in ("en cours", "en-cours", "en cours de livraison"):
                            new_v2_status = Order.EN_COURS
                    if linked_order.status in (Order.CONFIRMEE, Order.EN_COURS, Order.AU_MAGASIN):
                        if navex_lower in ("retour expediteur", "retour expéditeur",
                                           "retour vers expediteur", "retour vers expéditeur",
                                           "rtn client/agence", "rtn client", "rtn agence"):
                            new_v2_status = Order.RETURNING
                    if new_v2_status and new_v2_status != linked_order.status:
                        old_label = dict(Order.STATUS_CHOICES).get(linked_order.status, linked_order.status)
                        linked_order.status = new_v2_status
                        linked_order.save(update_fields=["status", "updated_at"])
                        log_action(
                            request.user, AuditLog.STATUS_CHANGE,
                            description=f"Auto sync Navex: commande #{linked_order.id} {old_label} → "
                                        f"{dict(Order.STATUS_CHOICES)[new_v2_status]} (bordereau {bc}, etat '{navex_etat}')",
                            request=request,
                        )
            except ShippingOrder.DoesNotExist:
                pass

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
            # 'Notre total' = the Navex price we saved on the order at scan time
            # (api_save_navex_info writes the Navex 'prix' to order.amount_collected).
            # We no longer recompute from sell_price + 7 — that would double-count
            # delivery and cause spurious mismatches with Navex's price.
            try:
                order_obj = ShippingOrder.objects.get(pk=order["id"])
                order_items = list(order_obj.items.select_related("unit__variant__product"))
                unit_count = len(order_items)
                our_total = order_obj.amount_collected  # may be None if not saved
            except Exception:
                our_total = None
                unit_count = 0

            price_match = None
            if navex_prix and our_total:
                try:
                    price_match = abs(Decimal(str(navex_prix)) - Decimal(str(our_total))) < Decimal("0.1")
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
    season = request.GET.get("season", "")  # 'summer', 'winter', or '' (= bubble landing)

    # If no season picked and not viewing archived → show the bubble landing page
    if not season and not show_archived:
        summer_count = Product.objects.filter(archived=False, season=Product.SEASON_SUMMER).count()
        winter_count = Product.objects.filter(archived=False, season=Product.SEASON_WINTER).count()
        archived_count = Product.objects.filter(archived=True).count()
        return render(request, "inventory/products_landing.html", {
            "summer_count": summer_count,
            "winter_count": winter_count,
            "archived_count": archived_count,
        })

    products_qs = Product.objects.prefetch_related("variants__units")
    if show_archived:
        products_qs = products_qs.filter(archived=True)
    else:
        products_qs = products_qs.filter(archived=False)
        if season in (Product.SEASON_SUMMER, Product.SEASON_WINTER):
            products_qs = products_qs.filter(season=season)
    products = products_qs.all()
    total_available = ProductUnit.objects.filter(status__in=(ProductUnit.IN_STOCK, ProductUnit.RETURNED)).count()
    total_shipped = ProductUnit.objects.filter(status=ProductUnit.SHIPPED).count()
    total_paid = ProductUnit.objects.filter(status=ProductUnit.PAID).count()
    total_early_return = ProductUnit.objects.filter(status=ProductUnit.EARLY_RETURN).count()
    total_at_depot = ProductUnit.objects.filter(status=ProductUnit.AT_DEPOT).count()

    from .models import compute_size_forecast

    # Calculate low stock sizes per product (predictive: days-of-cover < 10)
    products_data = []
    for product in products:
        # Stock breakdown: total disponible = in_stock + returned
        in_stock_count = 0
        returned_count = 0
        early_return_count = 0
        at_depot_count = 0
        for variant in product.variants.all():
            for unit in variant.units.all():
                if unit.status == ProductUnit.IN_STOCK:
                    in_stock_count += 1
                elif unit.status == ProductUnit.RETURNED:
                    returned_count += 1
                elif unit.status == ProductUnit.EARLY_RETURN:
                    early_return_count += 1
                elif unit.status == ProductUnit.AT_DEPOT:
                    at_depot_count += 1
        stock = in_stock_count + returned_count
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
            "in_stock_count": in_stock_count,
            "returned_count": returned_count,
            "early_return_count": early_return_count,
            "at_depot_count": at_depot_count,
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
        "total_early_return": total_early_return,
        "total_at_depot": total_at_depot,
        "show_archived": show_archived,
        "season": season,
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

    # Stock breakdown for the whole product (across all variants/sizes)
    in_stock_total = 0
    returned_total = 0
    early_return_total = 0
    at_depot_total = 0
    for variant in variants:
        for unit in variant.units.all():
            if unit.status == ProductUnit.IN_STOCK:
                in_stock_total += 1
            elif unit.status == ProductUnit.RETURNED:
                returned_total += 1
            elif unit.status == ProductUnit.EARLY_RETURN:
                early_return_total += 1
            elif unit.status == ProductUnit.AT_DEPOT:
                at_depot_total += 1
    available_total = in_stock_total + returned_total

    return render(request, "inventory/product_detail.html", {
        "product": product,
        "variants": variants,
        "variants_data": variants_data,
        "in_stock_total": in_stock_total,
        "returned_total": returned_total,
        "early_return_total": early_return_total,
        "at_depot_total": at_depot_total,
        "available_total": available_total,
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
    from datetime import date as _date
    from django.db.models import Q
    # Default behavior: show "non_confirmee" orders only.
    # User must explicitly pick a filter (or "all") to see other orders.
    status_filter = request.GET.get("status", None)
    if status_filter is None:
        status_filter = "non_confirmee"

    qs = Order.objects.select_related("customer", "region", "sales_page").prefetch_related(
        "lines__product", "order_offers", "shipping_orders"
    )
    if status_filter and status_filter != "all":
        qs = qs.filter(status=status_filter)

    # Hide orders scheduled for a future date (today's not-yet-actionable orders).
    # NULL scheduled_for = always visible. scheduled_for <= today = visible.
    # User can pass ?show_scheduled=1 to bypass this filter.
    today = _date.today()
    if request.GET.get("show_scheduled") != "1":
        qs = qs.filter(Q(scheduled_for__isnull=True) | Q(scheduled_for__lte=today))

    orders = qs[:500]

    # For each displayed order, how many TOTAL v2 orders share the same phone
    # number (i.e. same customer). Shown as a badge so staff can spot repeat
    # customers / possible duplicates. Computed in one query, no N+1.
    from django.db.models import Count as _Count
    customer_ids = {o.customer_id for o in orders if o.customer_id}
    order_counts = {}
    if customer_ids:
        for row in (Order.objects.filter(customer_id__in=customer_ids)
                    .exclude(status="annulee")
                    .values("customer_id").annotate(n=_Count("id"))):
            order_counts[row["customer_id"]] = row["n"]
    for o in orders:
        o.phone_order_count = order_counts.get(o.customer_id, 1)

    # Count of currently-hidden future-scheduled orders, for an info banner
    future_count = Order.objects.filter(scheduled_for__gt=today).count()

    from django.db.models import Count
    counts = dict(Order.objects.values_list("status").annotate(n=Count("id")))

    # If ?create_exchange=ID is in the URL, fetch the original order so the
    # template can pre-fill the inline editor for an exchange.
    exchange_source = None
    create_exchange_id = request.GET.get("create_exchange")
    if create_exchange_id:
        try:
            src = Order.objects.select_related("customer", "region", "sales_page").prefetch_related("lines__product", "order_offers").get(pk=int(create_exchange_id))
            # Only allow exchanges from delivered orders
            if src.status == Order.LIVREE:
                exchange_source = src
        except (Order.DoesNotExist, ValueError):
            pass

    return render(request, "inventory/orders_list.html", {
        "orders": orders,
        "status_filter": status_filter,
        "counts": counts,
        "total": Order.objects.count(),
        "future_count": future_count,
        "show_scheduled": request.GET.get("show_scheduled") == "1",
        # Data needed by the inline-create row + modal
        "sales_pages": SalesPage.objects.filter(is_active=True),
        "regions": Region.objects.filter(is_active=True),
        # Exchange source for pre-filling the editor
        "exchange_source": exchange_source,
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

        # Build the "family" of products that share the same physical SKU.
        # Whichever product the user picked when creating the offer (parent OR
        # V2 OR V3), find the root (parent_product OR self) then list all
        # products in the family.
        from .models import Product
        from django.db.models import Q
        root = product.parent_product or product
        family = Product.objects.filter(
            Q(id=root.id) | Q(parent_product=root)
        ).prefetch_related("variants__units")

        # Group variants by color across the whole family so V2 "noir" merges
        # with V1 "noir" — same physical color = one pick for the office user.
        from collections import OrderedDict
        by_color = OrderedDict()
        for fam_p in family:
            for v in fam_p.variants.all():
                color_key = (v.color_label or v.color_name or "").strip().lower() or f"var{v.id}"
                if color_key not in by_color:
                    by_color[color_key] = {
                        "id": v.id,  # representative variant id
                        # A single-colour product may have a blank colour; show
                        # "Unique" rather than an empty "—" so it's selectable.
                        "color": (v.color_label or v.color_name or "").strip() or "Unique",
                        "image_url": v.image.url if v.image else None,
                        "sizes": [],
                        "stock_by_size": {},
                        "variant_ids": [],  # all variant ids merged under this color
                    }
                entry = by_color[color_key]
                entry["variant_ids"].append(v.id)
                if not entry["image_url"] and v.image:
                    entry["image_url"] = v.image.url
                for u in v.units.all():
                    if u.size and u.size not in entry["sizes"]:
                        entry["sizes"].append(u.size)
                    if u.status in (ProductUnit.IN_STOCK, ProductUnit.RETURNED):
                        entry["stock_by_size"][u.size] = entry["stock_by_size"].get(u.size, 0) + 1

        variants = list(by_color.values())

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
            "is_active": offer.is_active,
            "sales_page_ids": list(offer.sales_pages.values_list("id", flat=True)),
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


# ---- AUTO-SAVE / DRAFT FLOW (Phase A) -------------------------------------
# These let the frontend save the order progressively as the user fills it in,
# instead of waiting for a "Save" button. The order is created as soon as the
# phone number is filled in; from then on every field change → autosave call.
# All edits are refused once status != non_confirmee (server-enforced lock).

def _is_draft_editable(order):
    """An order stays editable through the early call-center statuses
    (non confirmée, injoignable, pas sérieux, rappeler, annulée…).
    It locks only once it is CONFIRMÉE or LIVRÉE, or once it has been pushed
    to Navex (has a bordereau barcode)."""
    LOCKED_STATUSES = {"confirmee", "livree", "au_magasin", "en_cours"}
    if order.status in LOCKED_STATUSES:
        return False
    if order.bordereau_barcode:
        return False
    return True


@csrf_exempt
@require_POST
@login_required(login_url="/login/")
def api_order_draft_upsert(request):
    """Create or update a draft order.
    - If 'order_id' is in payload → update existing draft (with edit-lock check)
    - Otherwise → create new draft (requires phone + sales_page)

    Accepts ANY subset of fields. Unspecified fields aren't touched.
    Returns {status:'ok', order_id, status, total, locked: bool}.
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

    order_id = data.get("order_id")
    # Normalize phone: keep only digits (must match frontend cleanPhone()).
    # This way "12 345 678", "12.345.678", " 12345678 " all become "12345678".
    raw_phone = (data.get("phone") or "")
    phone = "".join(ch for ch in raw_phone if ch.isdigit())
    raw_phone2 = (data.get("phone2") or "")
    phone2 = "".join(ch for ch in raw_phone2 if ch.isdigit())
    name = (data.get("name") or "").strip()

    try:
        if order_id:
            # UPDATE existing draft
            try:
                order = Order.objects.select_related("customer").get(pk=order_id)
            except Order.DoesNotExist:
                return JsonResponse({"status": "error", "message": "Brouillon introuvable."}, status=404)
            if not _is_draft_editable(order):
                return JsonResponse({
                    "status": "error",
                    "message": "Cette commande n'est plus modifiable (déjà confirmée).",
                    "locked": True,
                }, status=400)
        else:
            # CREATE new draft — require phone (8 digits, Tunisian) + sales_page
            # Return 200 with "waiting" status instead of 400 so the frontend
            # doesn't spam errors before the user has finished typing.
            if not phone:
                return JsonResponse({"status": "waiting", "message": "Téléphone requis pour créer le brouillon."})
            if len(phone) != 8:
                return JsonResponse({"status": "waiting", "message": f"Téléphone invalide ({len(phone)} chiffres, 8 requis)."})
            sales_page_id = data.get("sales_page")
            if not sales_page_id:
                return JsonResponse({"status": "waiting", "message": "Page requise pour créer le brouillon."})
            # Normalize phone — strip whitespace, remove any hidden chars
            # to avoid creating a "duplicate" customer with phone=" 11111111".
            phone_norm = phone.strip()
            customer = Customer.objects.filter(phone=phone_norm).first()
            if customer is None:
                # Try to create. If a race condition or normalization issue causes
                # IntegrityError, fall back to the existing one.
                try:
                    customer = Customer.objects.create(phone=phone_norm, name=name)
                except Exception:
                    customer = Customer.objects.filter(phone=phone_norm).first()
                    if customer is None:
                        return JsonResponse({"status": "error", "message": f"Impossible de créer/trouver le client {phone_norm}."}, status=400)
            if name and customer.name != name:
                customer.name = name
                customer.save(update_fields=["name"])
            if phone2 and customer.phone2 != phone2:
                customer.phone2 = phone2
                customer.save(update_fields=["phone2"])
            order = Order.objects.create(
                customer=customer,
                sales_page_id=sales_page_id,
                created_by=request.user if request.user.is_authenticated else None,
                status="non_confirmee",
            )
            # If this is an exchange (frontend passes exchange_of_id), link the
            # new order to the original delivered order.
            exchange_of_id = data.get("exchange_of_id")
            if exchange_of_id:
                try:
                    src = Order.objects.get(pk=int(exchange_of_id))
                    if src.status == Order.LIVREE:
                        order.exchange_of_id = src.id
                        order.save(update_fields=["exchange_of"])
                except (Order.DoesNotExist, ValueError, TypeError):
                    pass
            log_action(
                request.user, AuditLog.CREATE,
                description=f"Brouillon créé (auto) — {phone_norm}" + (f" [échange de #{exchange_of_id}]" if exchange_of_id else ""),
                request=request, target_model="Order", target_id=order.id,
            )

        # Apply any provided fields
        changed = []
        # Customer fields
        if phone and order.customer and order.customer.phone != phone:
            order.customer.phone = phone
            order.customer.save(update_fields=["phone"])
            changed.append("phone")
        if name and order.customer and order.customer.name != name:
            order.customer.name = name
            order.customer.save(update_fields=["name"])
            changed.append("name")
        # Phone2 is optional — only update if the payload includes it (so blank
        # payload doesn't accidentally wipe an existing secondary number)
        if "phone2" in data and order.customer:
            new_phone2 = phone2  # already cleaned to digits-only above
            if order.customer.phone2 != new_phone2:
                order.customer.phone2 = new_phone2
                order.customer.save(update_fields=["phone2"])
                changed.append("phone2")
        # Direct order fields — only update if present in payload
        if "sales_page" in data and data["sales_page"]:
            order.sales_page_id = data["sales_page"]
            changed.append("sales_page")
        if "region" in data:
            order.region_id = data["region"] or None
            changed.append("region")
        if "ville" in data:
            order.ville = (data["ville"] or "").strip()
            changed.append("ville")
        if "localite" in data:
            order.localite = (data["localite"] or "").strip()
            changed.append("localite")
        if "address" in data:
            order.address = (data["address"] or "").strip()
            changed.append("address")
        if "delivery_fee" in data:
            try:
                order.delivery_fee = Decimal(str(data["delivery_fee"]))
                changed.append("delivery_fee")
            except Exception:
                pass
        # Phase 2.2: exchange fault toggle. "ours" → free shipping (0 DT);
        # "client" → standard 7 DT. Setting the fault auto-adjusts delivery_fee
        # unless the payload also explicitly set delivery_fee in the same call.
        if "exchange_fault" in data:
            fault = (data.get("exchange_fault") or "").strip()
            valid_faults = dict(Order.EXCHANGE_FAULT_CHOICES)
            if fault in valid_faults:
                order.exchange_fault = fault
                changed.append("exchange_fault")
                if "delivery_fee" not in data:
                    if fault == Order.EXCHANGE_FAULT_OURS:
                        order.delivery_fee = Decimal("0")
                        changed.append("delivery_fee")
                    elif fault == Order.EXCHANGE_FAULT_CLIENT:
                        order.delivery_fee = Decimal("7")
                        changed.append("delivery_fee")
        if "discount" in data:
            try:
                order.discount = Decimal(str(data["discount"]))
                changed.append("discount")
            except Exception:
                pass
        if "notes" in data:
            order.notes = (data["notes"] or "").strip()
            changed.append("notes")
        if changed:
            order.save()

        # Replace offers if the payload contains them (full replace, not partial)
        if "offers" in data:
            with transaction.atomic():
                # Wipe existing lines and offers, then rebuild from payload
                order.lines.all().delete()
                order.order_offers.all().delete()
                for op in data.get("offers", []):
                    offer_id = op.get("offer_id")
                    if not offer_id:
                        continue
                    try:
                        offer = Offer.objects.get(pk=offer_id)
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
                            unit_price=0,  # not used inside an offer (offer.bundle_price drives the total)
                        )
            changed.append("offers")

        # CRITICAL: recompute the order total after any change.
        # Without this, Order.total stays at 0 and we'd push prix=0 to Navex.
        order.recalc_total()

        return JsonResponse({
            "status": "ok",
            "order_id": order.id,
            "order_status": order.status,
            "total": str(order.total),
            "locked": not _is_draft_editable(order),
            "changed": changed,
        })
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)}, status=400)


@login_required(login_url="/login/")
def api_order_draft_get(request, pk):
    """Return the full state of an order as JSON, for re-opening the edit form."""
    from .models import Order
    if not _orders_role_check(request):
        return JsonResponse({"status": "error"}, status=403)
    try:
        order = Order.objects.select_related("customer", "sales_page", "region").prefetch_related(
            "order_offers", "lines"
        ).get(pk=pk)
    except Order.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Commande introuvable."}, status=404)

    offers_data = []
    for oo in order.order_offers.all():
        # Group lines belonging to this offer
        products = []
        for line in order.lines.filter(order_offer=oo):
            products.append({
                "offer_product_id": None,
                "product_id": line.product_id,
                "variant_id": line.variant_id,
                "size": line.size,
                "quantity": line.quantity,
            })
        offers_data.append({
            "offer_id": oo.offer_id,
            "quantity": oo.quantity,
            "products": products,
        })

    # Standalone product lines (no order_offer) — typically from Shopify webhook
    # when a single product (not a bundle) was purchased.
    standalone_lines = []
    for line in order.lines.filter(order_offer__isnull=True):
        standalone_lines.append({
            "product_id": line.product_id,
            "variant_id": line.variant_id,
            "size": line.size,
            "quantity": line.quantity,
            "unit_price": str(line.unit_price) if line.unit_price else "0",
        })

    return JsonResponse({
        "status": "ok",
        "order": {
            "id": order.id,
            "status": order.status,
            "status_display": order.get_status_display(),
            "locked": not _is_draft_editable(order),
            "phone": order.customer.phone if order.customer else "",
            "phone2": order.customer.phone2 if order.customer else "",
            "name": order.customer.name if order.customer else "",
            "sales_page": order.sales_page_id,
            "sales_page_name": order.sales_page.name if order.sales_page else "",
            "region": order.region_id,
            "region_name": order.region.name if order.region else "",
            "ville": order.ville,
            "localite": order.localite,
            "address": order.address,
            "delivery_fee": str(order.delivery_fee),
            "discount": str(order.discount),
            "notes": order.notes,
            "offers": offers_data,
            "standalone_lines": standalone_lines,
            "total": str(order.total),
            "article_summary": order.article_summary,
            "created_at": order.created_at.strftime("%d/%m %H:%M") if order.created_at else "",
            "exchange_of_id": order.exchange_of_id,
            "return_items_count": order.return_items.count() if order.exchange_of_id else 0,
        },
    })


# ---- Search API ------------------------------------------------------------

@login_required(login_url="/login/")
def api_orders_search(request):
    """Search orders across multiple fields. Returns up to 200 matches.

    Search logic:
      - If query starts with "#" → match exact order ID
      - If query is all digits → match phone, phone2, bordereau, or ID
      - Otherwise (text) → match customer name (case-insensitive contains)
    """
    from .models import Order
    if not _orders_role_check(request):
        return JsonResponse({"status": "error"}, status=403)

    q = (request.GET.get("q") or "").strip()
    if not q:
        return JsonResponse({"status": "ok", "results": []})

    from django.db.models import Q

    qs = Order.objects.select_related("customer", "region", "sales_page")

    if q.startswith("#"):
        try:
            order_id = int(q[1:])
            qs = qs.filter(pk=order_id)
        except ValueError:
            return JsonResponse({"status": "ok", "results": []})
    elif q.isdigit():
        try:
            order_id_match = int(q)
        except ValueError:
            order_id_match = None
        f = Q(customer__phone__icontains=q) | Q(customer__phone2__icontains=q) | Q(bordereau_barcode__icontains=q)
        if order_id_match is not None:
            f |= Q(pk=order_id_match)
        qs = qs.filter(f)
    else:
        qs = qs.filter(customer__name__icontains=q)

    qs = qs.order_by("-created_at")[:200]
    # Count total orders per customer (for the repeat-customer badge).
    from django.db.models import Count as _Count2
    cust_ids = {o.customer_id for o in qs if o.customer_id}
    pcounts = {}
    if cust_ids:
        for row in (Order.objects.filter(customer_id__in=cust_ids)
                    .exclude(status="annulee")
                    .values("customer_id").annotate(n=_Count2("id"))):
            pcounts[row["customer_id"]] = row["n"]
    results = []
    for o in qs:
        results.append({
            "id": o.id,
            "phone": o.customer.phone if o.customer else "",
            "phone2": o.customer.phone2 if o.customer else "",
            "phone_order_count": pcounts.get(o.customer_id, 1),
            "name": o.customer.name if o.customer else "",
            "status": o.status,
            "status_display": o.get_status_display(),
            "sales_page_name": o.sales_page.name if o.sales_page else "",
            "region_name": o.region.name if o.region else "",
            "ville": o.ville,
            "address": o.address or "",
            "total": str(o.total),
            "amount_collected": str(o.amount_collected) if o.amount_collected is not None else None,
            "status_note": o.status_note or "",
            "bordereau": o.bordereau_barcode,
            "navex_last_status": o.navex_last_status or "",
            "navex_last_synced_at": o.navex_last_synced_at.strftime("%d/%m %H:%M") if o.navex_last_synced_at else "",
            "article_summary": o.article_summary,
            "created_at": o.created_at.strftime("%d/%m %H:%M") if o.created_at else "",
            "scheduled_for": o.scheduled_for.strftime("%Y-%m-%d") if o.scheduled_for else "",
            "is_exchange": bool(o.exchange_of_id),
        })
    return JsonResponse({"status": "ok", "results": results, "count": len(results)})


# ---- Schedule an order for later --------------------------------------------

@csrf_exempt
@require_POST
@login_required(login_url="/login/")
def api_order_set_scheduled(request, pk):
    """Update the scheduled_for date on an order.
    Body: {scheduled_for: "YYYY-MM-DD"} or {scheduled_for: ""} to clear.
    """
    from .models import Order, log_action, AuditLog
    if not _orders_role_check(request):
        return JsonResponse({"status": "error"}, status=403)
    try:
        order = Order.objects.get(pk=pk)
    except Order.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Commande introuvable."}, status=404)
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "JSON invalide."}, status=400)
    raw = (data.get("scheduled_for") or "").strip()
    from datetime import datetime, date as _date
    new_date = None
    if raw:
        try:
            new_date = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            return JsonResponse({"status": "error", "message": "Format de date invalide (attendu YYYY-MM-DD)."}, status=400)
    old_date = order.scheduled_for
    order.scheduled_for = new_date
    order.save(update_fields=["scheduled_for", "updated_at"])
    log_action(
        request.user, AuditLog.EDIT,
        description=f"Date prévue changée : {old_date} → {new_date} pour commande #{order.id}",
        target_model="Order", target_id=order.id,
    )
    return JsonResponse({
        "status": "ok",
        "scheduled_for": new_date.strftime("%Y-%m-%d") if new_date else "",
        "display": new_date.strftime("%d/%m") if new_date else "—",
    })


# ---- Ads dashboard: spend (from Meta, per campaign) linked to offers --------

def _meta_fetch_spend_by_campaign(start_date, end_date):
    """Fetch ad spend PER CAMPAIGN from Meta between two dates (inclusive).
    Returns a dict {campaign_name: spend_float}. Empty dict on any error.

    Reuses the same env vars as the existing daily-total fetch:
    META_ACCESS_TOKEN and META_AD_ACCOUNT_ID.
    """
    import urllib.request
    token = os.environ.get("META_ACCESS_TOKEN", "").strip()
    account_id = os.environ.get("META_AD_ACCOUNT_ID", "").strip()
    if not token or not account_id:
        return {}
    if not account_id.startswith("act_"):
        account_id = f"act_{account_id}"
    url = (
        f"https://graph.facebook.com/v18.0/{account_id}/insights"
        f"?level=campaign&fields=campaign_name,spend"
        f"&time_range={{'since':'{start_date}','until':'{end_date}'}}"
        f"&limit=200&access_token={token}"
    )
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        result = {}
        for entry in data.get("data", []):
            name = entry.get("campaign_name", "")
            if not name:
                continue
            try:
                result[name] = float(entry.get("spend", "0"))
            except (ValueError, TypeError):
                result[name] = 0.0
        return result
    except Exception:
        return {}


def _sync_ads_from_meta(start_date, end_date):
    """Upsert Ad rows from Meta per-campaign spend. Returns (ok, message, n)."""
    from decimal import Decimal
    from django.utils import timezone
    from .models import Ad
    spend_map = _meta_fetch_spend_by_campaign(start_date, end_date)
    if not spend_map:
        return False, "Aucune donnée Meta (vérifier le token / la période).", 0
    now = timezone.now()
    n = 0
    for name, spend in spend_map.items():
        ad, _ = Ad.objects.get_or_create(campaign_name=name)
        ad.spend = Decimal(str(round(spend, 2)))
        ad.last_synced_at = now
        ad.save(update_fields=["spend", "last_synced_at", "updated_at"])
        n += 1
    return True, f"{n} publicité(s) synchronisée(s) depuis Meta.", n


@login_required(login_url="/login/")
def ads_offers_dashboard(request):
    """Ads page: shows ONE day (latest/today by default), with each campaign
    listed underneath its spend, a dropdown to link each campaign to an offer,
    and cross-source paid revenue per linked offer (web + DM + manual).
    """
    if not _orders_role_check(request):
        return redirect("home")
    from datetime import date, datetime
    from .models import Ad, Offer, Order

    # Single day. Default: today. ?day=YYYY-MM-DD to pick another day.
    today = date.today()
    try:
        day = datetime.strptime(request.GET.get("day", ""), "%Y-%m-%d").date()
    except ValueError:
        day = today
    day_str = day.strftime("%Y-%m-%d")

    # Sync that day's per-campaign spend from Meta.
    _sync_ads_from_meta(day_str, day_str)

    ads = list(Ad.objects.select_related("offer").all())
    offers = list(Offer.objects.filter(is_active=True).order_by("name"))

    # Paid orders CREATED on that day, grouped by offer (all sources).
    from collections import defaultdict
    paid_orders = (Order.objects
                   .filter(status=Order.LIVREE, created_at__date=day)
                   .prefetch_related("order_offers"))
    offer_orders = defaultdict(set)
    offer_revenue = defaultdict(lambda: Decimal("0"))
    for o in paid_orders:
        distinct_offers = {oo.offer_id for oo in o.order_offers.all() if oo.offer_id}
        for oid in distinct_offers:
            offer_orders[oid].add(o.id)
            offer_revenue[oid] += (o.total or Decimal("0"))

    rows = []
    total_spend = Decimal("0")
    total_revenue = Decimal("0")
    for ad in ads:
        spend = ad.spend or Decimal("0")
        total_spend += spend
        order_count = len(offer_orders.get(ad.offer_id, set())) if ad.offer_id else 0
        revenue = offer_revenue.get(ad.offer_id, Decimal("0")) if ad.offer_id else Decimal("0")
        if ad.offer_id:
            total_revenue += revenue
        rows.append({
            "ad": ad, "spend": spend, "order_count": order_count,
            "revenue": revenue, "profit": revenue - spend,
        })
    # Sort by spend desc (highest spenders first).
    rows.sort(key=lambda r: r["spend"], reverse=True)

    return render(request, "inventory/ads_offers.html", {
        "rows": rows,
        "offers": offers,
        "day": day_str,
        "is_today": day == today,
        "total_spend": total_spend,
        "total_revenue": total_revenue,
        "total_profit": total_revenue - total_spend,
        "unlinked": sum(1 for a in ads if not a.offer_id),
    })


@csrf_exempt
@require_POST
@login_required(login_url="/login/")
def api_ad_link_offer(request, pk):
    """Link (or unlink) an Ad to an Offer. Body: {"offer_id": <id> or null}."""
    if not _orders_role_check(request):
        return JsonResponse({"status": "error", "message": "Accès refusé."}, status=403)
    from .models import Ad, Offer
    try:
        ad = Ad.objects.get(pk=pk)
    except Ad.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Publicité introuvable."}, status=404)
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "JSON invalide."}, status=400)
    offer_id = data.get("offer_id")
    if offer_id in (None, "", "none"):
        ad.offer = None
    else:
        try:
            ad.offer = Offer.objects.get(pk=int(offer_id))
        except (Offer.DoesNotExist, ValueError, TypeError):
            return JsonResponse({"status": "error", "message": "Offre invalide."}, status=400)
    ad.save(update_fields=["offer", "updated_at"])
    return JsonResponse({"status": "ok", "ad_id": ad.id, "offer_id": ad.offer_id,
                         "offer_name": ad.offer.name if ad.offer_id else ""})


@login_required(login_url="/login/")
def diagnose_offer_web(request):
    """TEMPORARY admin-only diagnostic. Open /diagnose-offer/?name=Icy%20Maze
    (or ?offer_id=NN) in a browser. Prints offer -> product -> family ->
    variants -> unit counts by size as plain text. Remove after debugging."""
    if not _orders_role_check(request):
        return HttpResponse("Accès refusé.", status=403, content_type="text/plain; charset=utf-8")
    from .models import Offer, Product, ProductUnit
    from django.db.models import Q

    name = (request.GET.get("name") or "").strip()
    offer_id = (request.GET.get("offer_id") or "").strip()
    qs = Offer.objects.all()
    if offer_id.isdigit():
        qs = qs.filter(id=int(offer_id))
    elif name:
        qs = qs.filter(name__icontains=name)
    else:
        return HttpResponse("Ajoutez ?name=Icy Maze ou ?offer_id=NN à l'URL.",
                            content_type="text/plain; charset=utf-8")

    lines = []
    for offer in qs:
        lines.append(f"=== OFFER #{offer.id}: {offer.name} (active={offer.is_active}) ===")
        ops = offer.products.all()
        if not ops:
            lines.append("  (offer has NO products linked)")
        for op in ops:
            p = op.product
            root = p.parent_product or p
            family = Product.objects.filter(Q(id=root.id) | Q(parent_product=root))
            lines.append(f"  OfferProduct -> product #{p.id} '{p.name}' "
                         f"(parent={p.parent_product_id}) qty={op.quantity}")
            lines.append(f"    root=#{root.id} '{root.name}'  family size={family.count()}")
            for fam_p in family:
                variants = fam_p.variants.all()
                lines.append(f"      product #{fam_p.id} '{fam_p.name}' -> {variants.count()} variant(s)")
                for v in variants:
                    by_size = {}
                    for u in v.units.all():
                        by_size[u.size] = by_size.get(u.size, 0) + 1
                    lines.append(f"        variant #{v.id} color='{v.color_label or v.color_name}' "
                                 f"units={v.units.count()} by_size={by_size}")
        lines.append("")
    if not lines:
        lines = [f"No offer matched name='{name}' offer_id='{offer_id}'."]
    return HttpResponse("\n".join(lines), content_type="text/plain; charset=utf-8")


# ---- DM order intake (called by n8n / Messenger pipeline) -------------------

@csrf_exempt
@require_POST
def api_n8n_create_order_from_dm(request):
    """Create a PENDING order from a Messenger DM, called by n8n.

    This is machine-to-machine (no user login). It is protected by a shared
    secret header instead: n8n must send  X-DM-Token: <DM_INTAKE_TOKEN>
    matching the env var, otherwise the request is rejected.

    Expected JSON:
    {
      "phone": "29876313",            # required (customer key)
      "name": "Alaeddine",            # optional
      "psid": "PSID_123",             # Messenger Page-Scoped ID (linking key)
      "conversation": "full chat...", # the conversation text to attach
      "source_ad": "120244285501770755",  # optional ad id for attribution
      "sales_page": <id>              # optional; falls back to default if omitted
    }

    Deliberate design:
    - Creates a NON_CONFIRMEE (pending) order only. NEVER auto-confirmed — a
      human reviews and completes it. This is the spec's golden rule for COD.
    - Stores conversation_text + conversation_updated_at on the order, and
      customer_psid on the customer (permanent linking key).
    - Does NOT try to build order lines here. It captures the customer +
      conversation + ad source as a pending draft; the team completes the
      articles. (AI line-item extraction is a later, separate layer.)
    """
    import os
    from django.utils import timezone
    from .models import Order, Customer, SalesPage, log_action, AuditLog

    # --- auth: shared secret header ---
    expected = (os.environ.get("DM_INTAKE_TOKEN") or "").strip()
    provided = (request.headers.get("X-DM-Token") or "").strip()
    if not expected or provided != expected:
        return JsonResponse({"status": "error", "message": "Unauthorized."}, status=401)

    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "JSON invalide."}, status=400)

    phone = (data.get("phone") or "").strip()
    if not phone:
        return JsonResponse({"status": "error", "message": "Téléphone obligatoire."}, status=400)

    name = (data.get("name") or "").strip()
    psid = (data.get("psid") or "").strip()
    conversation = (data.get("conversation") or "").strip()
    source_ad = (data.get("source_ad") or "").strip()

    try:
        # Customer: same phone = same customer. Update name/psid if provided.
        customer, _created = Customer.objects.get_or_create(phone=phone, defaults={"name": name})
        changed = []
        if name and customer.name != name:
            customer.name = name; changed.append("name")
        if psid and customer.customer_psid != psid:
            customer.customer_psid = psid; changed.append("customer_psid")
        if changed:
            customer.save(update_fields=changed)

        # Sales page: use provided, else first available (best-effort default).
        sales_page_id = data.get("sales_page")
        if not sales_page_id:
            sp = SalesPage.objects.first()
            sales_page_id = sp.id if sp else None

        # Note line records the ad source for traceability even before a
        # dedicated attribution field exists.
        note_bits = ["[Commande créée depuis Messenger]"]
        if source_ad:
            note_bits.append(f"Ad source: {source_ad}")

        order = Order.objects.create(
            customer=customer,
            sales_page_id=sales_page_id,
            status=Order.NON_CONFIRMEE,
            notes="\n".join(note_bits),
            conversation_text=conversation,
            conversation_updated_at=timezone.now() if conversation else None,
            created_by=None,
        )

        log_action(
            None, AuditLog.CREATE,
            description=f"Commande #{order.id} créée depuis Messenger pour {customer} (en attente de confirmation)",
            target_model="Order", target_id=order.id,
        )
        return JsonResponse({
            "status": "ok",
            "order_id": order.id,
            "customer_id": customer.id,
            "message": "Commande en attente créée.",
        })
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)}, status=400)


# ---- DM conversation: refresh / fetch latest --------------------------------

@csrf_exempt
@require_POST
@login_required(login_url="/login/")
def api_order_refresh_conversation(request, pk):
    """Return the latest stored Messenger conversation for this order.

    For now this simply returns what is stored on the order. It is the hook
    where a live re-fetch will plug in later: when the Messenger webhook /
    n8n integration is live, this endpoint can call out to pull the current
    conversation for the customer's PSID and update conversation_text.

    Honest constraint (see v2 spec): a live re-fetch only reliably works when
    the customer has sent a recent message (open messaging window). It cannot
    pull an arbitrary silent old conversation on demand.
    """
    from .models import Order
    if not _orders_role_check(request):
        return JsonResponse({"status": "error", "message": "Accès refusé."}, status=403)
    try:
        order = Order.objects.select_related("customer").get(pk=pk)
    except Order.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Commande introuvable."}, status=404)

    psid = (order.customer.customer_psid or "").strip() if order.customer_id else ""

    # --- Live re-fetch hook (future) ---------------------------------------
    # When the Messenger integration is live, attempt a fresh pull here, e.g.:
    #     new_text = _messenger_fetch_conversation(psid)
    #     if new_text:
    #         order.conversation_text = new_text
    #         order.conversation_updated_at = timezone.now()
    #         order.save(update_fields=["conversation_text",
    #                                   "conversation_updated_at", "updated_at"])
    # For now we just return whatever is stored.

    if order.conversation_text:
        return JsonResponse({
            "status": "ok",
            "conversation_text": order.conversation_text,
            "updated_at": order.conversation_updated_at.strftime("%d/%m/%Y %H:%M") if order.conversation_updated_at else "",
        })
    if psid:
        return JsonResponse({
            "status": "ok",
            "conversation_text": "",
            "message": "Aucune conversation enregistrée. Elle réapparaîtra si le client envoie un nouveau message.",
        })
    return JsonResponse({
        "status": "ok",
        "conversation_text": "",
        "message": "Cette commande n'est pas liée à une conversation Messenger.",
    })


# ---- Exchange: return items APIs --------------------------------------------

@login_required(login_url="/login/")
def api_exchange_source_items(request, pk):
    """For an EXCHANGE order, returns the list of items that were in the
    original delivered order (so the office worker can pick which ones the
    client returns).
    """
    from .models import Order
    if not _orders_role_check(request):
        return JsonResponse({"status": "error"}, status=403)
    try:
        exchange = Order.objects.select_related("exchange_of").get(pk=pk)
    except Order.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Commande introuvable."}, status=404)
    if not exchange.exchange_of_id:
        return JsonResponse({"status": "error", "message": "Cette commande n'est pas un échange."}, status=400)

    source = exchange.exchange_of
    items = []
    # Walk through the original order's OrderLines.
    # Each OrderLine has product, variant, size, quantity directly — NO physical unit
    # at this stage (units get attached later during scan expedition).
    # Group by (variant, size) so we get one card per "variant + size" combo.
    from collections import OrderedDict
    grouped = OrderedDict()
    for line in source.lines.select_related("product", "variant").all():
        variant_id = line.variant_id
        size = line.size or ""
        # Combine quantity across multiple lines with the same variant+size
        key = (variant_id, size)
        if key not in grouped:
            grouped[key] = {
                "variant_id": variant_id,
                "size": size,
                "product_name": line.product.name if line.product else "",
                "color_label": line.variant.color_label if line.variant else "",
                "image_url": line.variant.image.url if line.variant and line.variant.image else None,
                "qty": 0,
            }
        grouped[key]["qty"] += line.quantity or 1

    items = list(grouped.values())

    # Attach the physical unit barcodes from the original order, matched by
    # variant + size. Units are reachable via the v2 Order's v1 ShippingOrders
    # → OrderItems → unit. This lets the office worker see which barcode(s)
    # correspond to each item being returned.
    barcodes_by_key = {}
    for so in source.shipping_orders.all():
        for oi in so.items.select_related("unit__variant").all():
            unit = oi.unit
            if not unit:
                continue
            k = (unit.variant_id, unit.size or "")
            barcodes_by_key.setdefault(k, []).append({
                "barcode": unit.barcode,
                "status": unit.status,
                "status_label": unit.get_status_display(),
            })
    for it in items:
        it["units"] = barcodes_by_key.get((it["variant_id"], it["size"]), [])

    # Also check what's already been selected (if return_items already exist for this exchange)
    selected = list(exchange.return_items.values_list("variant_id", "size"))
    selected_set = set((vid, sz) for vid, sz in selected)
    for it in items:
        key = (it["variant_id"], it["size"])
        it["is_selected"] = key in selected_set

    return JsonResponse({
        "status": "ok",
        "source_order_id": source.id,
        "items": items,
    })


@csrf_exempt
@require_POST
@login_required(login_url="/login/")
def api_exchange_set_returns(request, pk):
    """Save the list of items the customer is returning for this exchange.
    Body: {items: [{variant_id, size, qty}, ...]}
    """
    from .models import Order, ExchangeReturnItem, ProductVariant
    if not _orders_role_check(request):
        return JsonResponse({"status": "error"}, status=403)
    try:
        exchange = Order.objects.select_related("exchange_of").get(pk=pk)
    except Order.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Commande introuvable."}, status=404)
    if not exchange.exchange_of_id:
        return JsonResponse({"status": "error", "message": "Cette commande n'est pas un échange."}, status=400)
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "JSON invalide."}, status=400)

    raw_items = data.get("items") or []
    # Wipe existing return_items and re-create from the new selection
    exchange.return_items.all().delete()

    created = 0
    for it in raw_items:
        try:
            variant_id = int(it.get("variant_id"))
            size = (it.get("size") or "").strip()
            qty = int(it.get("qty") or 1)
        except (TypeError, ValueError):
            continue
        try:
            variant = ProductVariant.objects.select_related("product").get(pk=variant_id)
        except ProductVariant.DoesNotExist:
            continue
        # Create N rows, one per unit expected back
        for _ in range(qty):
            ExchangeReturnItem.objects.create(
                exchange_order=exchange,
                variant=variant,
                size=size,
                product_name_snapshot=variant.product.name,
            )
            created += 1

    return JsonResponse({
        "status": "ok",
        "created": created,
        "total": exchange.return_items.count(),
    })


# ---- Shopify webhook receiver -----------------------------------------------

@csrf_exempt
@require_POST
def api_shopify_webhook_order_created(request):
    """Receive a Shopify 'order/create' webhook and create a v2 Order draft.

    Workflow:
      1. Verify HMAC signature (env var SHOPIFY_WEBHOOK_SECRET)
      2. Parse Shopify's JSON order payload
      3. Match each line item to a Product by fuzzy name (case-insensitive)
      4. Create Customer + Order (status=non_confirmee) + OrderLines
      5. Log to AuditLog

    Auth: NONE — Shopify can't login. Trust comes from the HMAC signature.
    """
    import hmac, hashlib, base64
    from .models import (
        Customer, Order, OrderLine, Product, ProductVariant,
        SalesPage, Region, AuditLog, log_action,
    )

    # 1. Verify HMAC signature
    secret = os.environ.get("SHOPIFY_WEBHOOK_SECRET", "")
    received_hmac = request.headers.get("X-Shopify-Hmac-Sha256", "")
    if not secret:
        # If the env var isn't set, refuse the request — never accept unsigned data
        return JsonResponse({"status": "error", "message": "Webhook secret non configuré."}, status=503)
    body = request.body
    computed = base64.b64encode(
        hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    ).decode("utf-8")
    if not hmac.compare_digest(computed, received_hmac):
        # Bad signature — could be an attacker
        log_action(
            None, AuditLog.OTHER,
            description=f"Webhook Shopify REJETÉ : signature HMAC invalide (IP {request.META.get('REMOTE_ADDR', '?')})",
        )
        return JsonResponse({"status": "error", "message": "Signature invalide."}, status=401)

    # 2. Parse payload
    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"status": "error", "message": "Payload JSON invalide."}, status=400)

    shopify_order_id = str(payload.get("id") or "")
    shopify_order_number = str(payload.get("order_number") or payload.get("name") or "")

    # 3. Extract customer info
    # Shopify's customer object is in `customer` (registered) or `billing_address` / `shipping_address`
    shipping = payload.get("shipping_address") or {}
    billing = payload.get("billing_address") or {}
    customer_data = payload.get("customer") or {}

    phone_raw = (
        shipping.get("phone")
        or billing.get("phone")
        or customer_data.get("phone")
        or payload.get("phone")
        or ""
    )
    # Normalize Tunisian phone: strip everything except digits, take last 8 digits
    phone_digits = "".join(c for c in str(phone_raw) if c.isdigit())
    if len(phone_digits) >= 8:
        phone_norm = phone_digits[-8:]
    else:
        phone_norm = phone_digits
    if not phone_norm:
        # Without a phone we can't deliver — log and bail
        log_action(
            None, AuditLog.OTHER,
            description=f"Webhook Shopify reçu pour #{shopify_order_number} mais SANS téléphone — ignoré.",
        )
        return JsonResponse({"status": "ok", "message": "No phone, ignored."})

    def _safe_name(d):
        """Build 'first last' from a Shopify address dict, filtering out
        None / 'None' / empty strings (which the customer sometimes leaves)."""
        if not d:
            return ""
        first = (d.get("first_name") or "").strip()
        last = (d.get("last_name") or "").strip()
        # Some forms send the literal string "None" or "null"
        if first.lower() in ("none", "null"):
            first = ""
        if last.lower() in ("none", "null"):
            last = ""
        parts = [p for p in (first, last) if p]
        return " ".join(parts)

    name = (
        _safe_name(shipping)
        or _safe_name(billing)
        or _safe_name(customer_data)
    )
    address1_raw = shipping.get("address1") or billing.get("address1") or ""
    address2_raw = shipping.get("address2") or billing.get("address2") or ""
    city_raw = (shipping.get("city") or billing.get("city") or "").strip()
    province_raw = (shipping.get("province") or billing.get("province") or "").strip()

    # ---- Arabic → French translation (covers ALL fields, not just province) ----
    # The customer may type the region/city in any field, in Arabic or French.
    # We translate everything first, then search for a matching Region/Delegation
    # across ALL fields (province, city, address1, address2).
    import unicodedata, re

    arabic_to_french = {
        # === 24 Governorates ===
        "تونس": "Tunis", "أريانة": "Ariana", "اريانة": "Ariana",
        "بن عروس": "Ben Arous", "بنعروس": "Ben Arous",
        "منوبة": "Manouba", "منوّبة": "Manouba",
        "نابل": "Nabeul", "زغوان": "Zaghouan",
        "بنزرت": "Bizerte", "باجة": "Beja", "جندوبة": "Jendouba",
        "الكاف": "Kef", "سليانة": "Siliana",
        "القيروان": "Kairouan", "القصرين": "Kasserine",
        "سيدي بوزيد": "Sidi Bouzid", "سيديبوزيد": "Sidi Bouzid",
        "سوسة": "Sousse", "المنستير": "Monastir", "المهدية": "Mahdia",
        "صفاقس": "Sfax", "صفاڤس": "Sfax",
        "قفصة": "Gafsa", "ڤفصة": "Gafsa",
        "توزر": "Tozeur", "قبلي": "Kebili", "ڤبلي": "Kebili",
        "قابس": "Gabes", "ڤابس": "Gabes",
        "مدنين": "Medenine", "تطاوين": "Tataouine",
        # === Delegations / common cities ===
        "الحمامات": "Hammamet", "نابول": "Nabeul",
        "جربة": "Houmt Souk", "حومة السوق": "Houmt Souk",
        "الرڤاب": "Regueb", "الرقاب": "Regueb",
        "أنفيدها": "Enfidha", "النفيضة": "Enfidha",
        "نفطة": "Nefta", "النفطة": "Nefta",
        "دوز": "Douz", "زرزيس": "Zarzis", "جرجيس": "Zarzis",
        "بن قردان": "Ben Gardane", "بنقردان": "Ben Gardane",
        "المرسى": "La Marsa", "قرطاج": "Carthage",
        "سيدي بوسعيد": "Sidi Bou Said", "قمرت": "Gammarth",
        "حلق الوادي": "La Goulette", "رادس": "Rades",
        "حمام الأنف": "Hammam Lif", "حمام الشط": "Hammam Chatt",
        "حمام سوسة": "Hammam Sousse", "أكودة": "Akouda",
        "هرڤلة": "Hergla", "بوفيشة": "Bouficha",
        "سيدي بوعلي": "Sidi Bou Ali", "المساكن": "Msaken",
        "القنطاوي": "Port El Kantaoui",
        "متلوي": "Metlaoui", "أم العرائس": "Oum Larayes",
        "الرديف": "Redeyef",
        "بوسالم": "Bou Salem", "طبرقة": "Tabarka",
        "عين دراهم": "Ain Draham",
        "تستور": "Testour", "ماطر": "Mateur",
        "منزل بورقيبة": "Menzel Bourguiba", "منزل جميل": "Menzel Jemil",
        "غار الملح": "Ghar El Melh",
        "رأس الجبل": "Ras Jebel",
        "قليبية": "Kelibia", "منزل تميم": "Menzel Temime",
        "قربة": "Korba", "بني خلاد": "Beni Khalled",
        "سليمان": "Soliman", "قرمبالية": "Grombalia",
        "دار شعبان الفهري": "Dar Chaabane",
        "بنبلة": "Bembla", "جمال": "Jemmal",
        "قصور الساف": "Ksour Essef", "الجم": "El Jem",
        "بومرداس": "Boumerdes",
        "مكنين": "Moknine", "تبلبة": "Teboulba",
        "جبنيانة": "Jebeniana",
        "الحامة": "El Hamma", "مارث": "Mareth",
        "متماطة": "Matmata",
        "المظيلة": "Mdhilla", "السند": "Sened",
        "الڤطار": "El Guettar",
        "الذكارة": "Dkhara",
        "سيدي صالح": "Sidi Saleh",
    }

    # Arabic letter → Latin transliteration (Tunisian-style).
    # Used as a FALLBACK after the named-place dictionary, so unknown words
    # (street names, neighborhoods, family names) are written in latin script
    # instead of being left in Arabic.
    arabic_translit = {
        # Hamzas / alif variants
        "ء": "'", "آ": "a", "أ": "a", "ؤ": "u", "إ": "i", "ئ": "i", "ا": "a",
        # Letters
        "ب": "b", "ة": "a", "ت": "t", "ث": "th",
        "ج": "j", "ح": "h", "خ": "kh",
        "د": "d", "ذ": "dh",
        "ر": "r", "ز": "z",
        "س": "s", "ش": "ch",
        "ص": "s", "ض": "dh",
        "ط": "t", "ظ": "dh",
        "ع": "a", "غ": "gh",
        "ف": "f",
        "ق": "k", "ڨ": "g", "ڤ": "g",  # Tunisian Q→G variants
        "ك": "k",
        "ل": "l", "م": "m", "ن": "n",
        "ه": "h",
        "و": "w", "ى": "a", "ي": "y",
        # Diacritics — strip
        "َ": "", "ُ": "", "ِ": "", "ّ": "", "ْ": "", "ً": "", "ٌ": "", "ٍ": "",
        # Arabic-Indic digits
        "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
        "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
        # Common punctuation
        "،": ",", "؛": ";", "؟": "?", "ـ": "",
    }

    def _transliterate_arabic_chars(text):
        """Convert any remaining Arabic letters to their latin equivalents
        (mechanical fallback when Gemini is unavailable)."""
        if not text:
            return ""
        out = []
        for ch in text:
            if "\u0600" <= ch <= "\u06ff":
                out.append(arabic_translit.get(ch, ""))
            else:
                out.append(ch)
        return "".join(out)

    def _gemini_transliterate(text):
        """Call Google Gemini API to transliterate Arabic → Latin (Tunisian style).
        Returns the transliterated text, or None on failure (caller falls back).
        Supports both classic API keys (AIza...) and new-format ones (AQ...).
        """
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key or not text:
            return None
        import urllib.request as _ureq
        import json as _json
        prompt = (
            "Translitère ce texte arabe (tunisien) en lettres latines lisibles "
            "(style phonétique français). N'ajoute aucun commentaire, aucune ponctuation "
            "supplémentaire, aucune explication. Réponds UNIQUEMENT avec le texte translittéré. "
            "Si une partie est déjà en latin, garde-la telle quelle.\n\n"
            f"Texte : {text}"
        )
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": 256,
            },
        }
        base_url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-2.5-flash-lite:generateContent"
        )
        data = _json.dumps(body).encode("utf-8")

        # Decide auth scheme based on key prefix.
        # - "AIza..." → classic API key, passed via ?key=
        # - "AQ..." or anything else → OAuth-style token, passed via Authorization: Bearer
        attempts = []
        if api_key.startswith("AIza"):
            attempts.append(("query", base_url + "?key=" + api_key, None))
        else:
            # Try Bearer first, then x-goog-api-key header as fallback
            attempts.append(("bearer", base_url, {"Authorization": "Bearer " + api_key}))
            attempts.append(("header", base_url, {"x-goog-api-key": api_key}))
            attempts.append(("query", base_url + "?key=" + api_key, None))

        last_error = None
        for mode, url, headers in attempts:
            # Try up to 2 times with backoff on 429
            for retry_attempt in range(2):
                try:
                    req = _ureq.Request(url, data=data, method="POST")
                    req.add_header("Content-Type", "application/json")
                    if headers:
                        for h, v in headers.items():
                            req.add_header(h, v)
                    with _ureq.urlopen(req, timeout=8) as resp:
                        resp_data = _json.loads(resp.read().decode("utf-8"))
                    candidates = resp_data.get("candidates") or []
                    if not candidates:
                        last_error = "no candidates"
                        break
                    parts = candidates[0].get("content", {}).get("parts") or []
                    if not parts:
                        last_error = "no parts"
                        break
                    result = (parts[0].get("text") or "").strip()
                    if result:
                        return result
                    break
                except Exception as e:
                    last_error = f"{mode}: {type(e).__name__}: {e}"
                    # If 429, wait briefly and retry once
                    if "429" in str(e) and retry_attempt == 0:
                        import time as _time
                        _time.sleep(2)
                        continue
                    break

        # All attempts failed
        try:
            import logging
            logging.getLogger(__name__).warning("Gemini transliteration failed: %s", last_error)
        except Exception:
            pass
        return None

    def _translate_arabic(text):
        """Replace any Arabic word in `text` with its French equivalent if known.
        Words we don't know are TRANSLITERATED via Gemini (batched per webhook,
        see _gemini_batch_translit) or mechanical fallback.
        """
        if not text:
            return ""
        # If no Arabic chars, return as-is
        if not any("\u0600" <= c <= "\u06ff" for c in text):
            return text
        # Try whole-string match first (for multi-word keys like "سيدي بوزيد")
        stripped = text.strip()
        if stripped in arabic_to_french:
            return arabic_to_french[stripped]
        # Multi-word substring replacement
        result = text
        for k in sorted(arabic_to_french.keys(), key=lambda x: -len(x)):
            if k in result:
                result = result.replace(k, " " + arabic_to_french[k] + " ")
        # Don't call Gemini per-field here. The webhook will batch all remaining
        # Arabic fields in ONE call (see _batch_transliterate below) to stay
        # under per-second quota.
        result = re.sub(r"\s+", " ", result).strip()
        return result

    def _batch_transliterate(fields_dict):
        """Take a dict {name: text} where some texts may contain Arabic.
        Make ONE Gemini call with all Arabic fields, then for each field that
        was still Arabic, replace it with the AI result.
        Fallback to mechanical translit if Gemini fails.

        Returns the updated dict (only Arabic-containing fields are modified).
        """
        # Filter out fields that don't contain Arabic
        arabic_items = [(k, v) for k, v in fields_dict.items()
                        if v and any("\u0600" <= c <= "\u06ff" for c in v)]
        if not arabic_items:
            return fields_dict
        # Build a single prompt with numbered items
        numbered = "\n".join(f"{i+1}. {v}" for i, (_, v) in enumerate(arabic_items))
        prompt = (
            "Translitère les textes arabes (tunisiens) suivants en lettres latines "
            "lisibles (style phonétique français). Garde le numéro et l'ordre. "
            "N'ajoute aucun commentaire, aucune explication. "
            "Si une partie est déjà en latin, garde-la telle quelle.\n\n"
            f"{numbered}"
        )
        ai_response = _gemini_transliterate(prompt)
        if ai_response:
            # Parse the numbered response back
            translated_by_idx = {}
            for line in ai_response.split("\n"):
                line = line.strip()
                if not line:
                    continue
                m = re.match(r"^(\d+)\.\s*(.+)$", line)
                if m:
                    translated_by_idx[int(m.group(1))] = m.group(2).strip()
            # Apply translations if we got the right count
            for i, (k, _v) in enumerate(arabic_items):
                idx = i + 1
                if idx in translated_by_idx:
                    candidate = translated_by_idx[idx]
                    # Only use Gemini result if it actually removed Arabic
                    if not any("\u0600" <= c <= "\u06ff" for c in candidate):
                        fields_dict[k] = candidate
                        continue
                # Fallback mechanical
                fields_dict[k] = _transliterate_arabic_chars(fields_dict[k])
        else:
            # Gemini failed entirely — mechanical for all
            for k, _v in arabic_items:
                fields_dict[k] = _transliterate_arabic_chars(fields_dict[k])
        return fields_dict

    # Translate ALL location fields (dictionary pass)
    province = _translate_arabic(province_raw)
    city = _translate_arabic(city_raw)
    address1 = _translate_arabic(address1_raw)
    address2 = _translate_arabic(address2_raw)
    # Also handle the customer name
    name = _translate_arabic(name)
    # Batch Gemini call for any remaining Arabic across all fields
    _batched = _batch_transliterate({
        "name": name,
        "province": province,
        "city": city,
        "address1": address1,
        "address2": address2,
    })
    name = _batched["name"]
    province = _batched["province"]
    city = _batched["city"]
    address1 = _batched["address1"]
    address2 = _batched["address2"]
    address = (address1 + (" " + address2 if address2 else "")).strip()

    # 4. Map region: find a Region/Delegation matching ANY of the location
    # fields (province, city, address1, address2). All fields have already
    # been translated from Arabic above, so matching is purely text-based.
    region = None
    matched_delegation_name = None

    def _normalize(s):
        """Lowercase, strip accents, remove filler words & punctuation."""
        if not s:
            return ""
        s = unicodedata.normalize("NFKD", s)
        s = "".join(c for c in s if not unicodedata.combining(c))
        s = s.lower()
        for w in ("governorate", "gouvernorat", "gouvernement", "wilaya", "province"):
            s = s.replace(w, "")
        s = re.sub(r"[^a-z0-9 ]", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s

    def _levenshtein(a, b):
        if a == b:
            return 0
        if not a:
            return len(b)
        if not b:
            return len(a)
        prev_row = list(range(len(b) + 1))
        for i, ca in enumerate(a):
            cur_row = [i + 1]
            for j, cb in enumerate(b):
                cost = 0 if ca == cb else 1
                cur_row.append(min(cur_row[j] + 1, prev_row[j + 1] + 1, prev_row[j] + cost))
            prev_row = cur_row
        return prev_row[-1]

    # Collect all candidate texts (translated) — try province first since it's
    # the most reliable signal, then city, then addresses.
    candidate_texts = [t for t in (province, city, address1, address2) if t]
    candidate_texts_norm = [_normalize(t) for t in candidate_texts]

    all_regions = list(Region.objects.filter(is_active=True))
    from .models import Delegation
    all_delegations = list(Delegation.objects.filter(is_active=True).select_related("region"))

    # Strategy A: exact match on a Region name in any candidate
    for cand_norm in candidate_texts_norm:
        if not cand_norm:
            continue
        for r in all_regions:
            if _normalize(r.name) == cand_norm:
                region = r
                break
        if region:
            break

    # Strategy B: Region name contained in a candidate (or candidate contains Region)
    if not region:
        for cand_norm in candidate_texts_norm:
            if not cand_norm:
                continue
            for r in all_regions:
                r_norm = _normalize(r.name)
                if r_norm and (r_norm in cand_norm or cand_norm in r_norm):
                    region = r
                    break
            if region:
                break

    # Strategy C: exact match on a Delegation name in any candidate
    if not region:
        for cand_norm in candidate_texts_norm:
            if not cand_norm:
                continue
            for d_obj in all_delegations:
                if _normalize(d_obj.name) == cand_norm:
                    region = d_obj.region
                    matched_delegation_name = d_obj.name
                    break
            if region:
                break

    # Strategy D: Delegation name contained in a candidate (or vice versa)
    if not region:
        for cand_norm in candidate_texts_norm:
            if not cand_norm:
                continue
            for d_obj in all_delegations:
                d_norm = _normalize(d_obj.name)
                if d_norm and len(d_norm) >= 3 and (d_norm in cand_norm or cand_norm in d_norm):
                    region = d_obj.region
                    matched_delegation_name = d_obj.name
                    break
            if region:
                break

    # Strategy E: fuzzy match (typos) — try Regions first, then Delegations.
    # Checks ALL candidate fields (province, city, address1, address2), not just
    # province, because the governorate name sometimes lands in the city/address
    # field (e.g. city="Mednine" — a spelling variant of "Médenine").
    if not region and candidate_texts_norm:
        # Regions: best fuzzy match across every candidate field.
        best = None
        best_dist = 999
        for cand_norm in candidate_texts_norm:
            if not cand_norm:
                continue
            for r in all_regions:
                r_norm = _normalize(r.name)
                if not r_norm:
                    continue
                d = _levenshtein(cand_norm, r_norm)
                threshold = 2 if len(r_norm) <= 6 else 3
                if d <= threshold and d < best_dist:
                    best = r
                    best_dist = d
        if best:
            region = best
        else:
            # Delegations: best fuzzy match across every candidate field.
            best_dleg = None
            best_dist = 999
            for cand_norm in candidate_texts_norm:
                if not cand_norm:
                    continue
                for d_obj in all_delegations:
                    d_norm = _normalize(d_obj.name)
                    if not d_norm:
                        continue
                    dd = _levenshtein(cand_norm, d_norm)
                    threshold = 2 if len(d_norm) <= 6 else 3
                    if dd <= threshold and dd < best_dist:
                        best_dleg = d_obj
                        best_dist = dd
            if best_dleg:
                region = best_dleg.region
                matched_delegation_name = best_dleg.name

    # Strategy F: we have a region but no matched delegation yet (e.g. province
    # had the gov name directly, like "SFAX"). Look in the other fields for
    # a delegation that BELONGS to that region — that's likely the city.
    # E.g. province="SFAX" + address1="jbenyana" → find Delegation "Jebeniana"
    # in Sfax region (via fuzzy match) and use that as ville.
    if region and not matched_delegation_name and candidate_texts_norm:
        # Skip the first candidate (province) since we already used it for the region.
        # Look at city, address1, address2.
        region_delegations = [d for d in all_delegations if d.region_id == region.id]
        # First: exact / contained
        for cand_norm in candidate_texts_norm[1:]:
            if not cand_norm:
                continue
            for d_obj in region_delegations:
                d_norm = _normalize(d_obj.name)
                if not d_norm:
                    continue
                if d_norm == cand_norm or (len(d_norm) >= 3 and (d_norm in cand_norm or cand_norm in d_norm)):
                    matched_delegation_name = d_obj.name
                    break
            if matched_delegation_name:
                break
        # Then: fuzzy
        if not matched_delegation_name:
            best_match = None
            best_score = 999
            for cand_norm in candidate_texts_norm[1:]:
                if not cand_norm or len(cand_norm) < 3:
                    continue
                for d_obj in region_delegations:
                    d_norm = _normalize(d_obj.name)
                    if not d_norm or len(d_norm) < 3:
                        continue
                    dist = _levenshtein(cand_norm, d_norm)
                    threshold = 2 if len(d_norm) <= 6 else 3
                    if dist <= threshold and dist < best_score:
                        best_match = d_obj
                        best_score = dist
            if best_match:
                matched_delegation_name = best_match.name

    # Strategy G: AI fallback — invoked when:
    # - We don't have a clear region+ville (classic failed), OR
    # - Shopify province exists but matched region disagrees (mismatch).
    province_conflict = False
    if region and province:
        prov_norm = _normalize(province)
        reg_norm = _normalize(region.name)
        if prov_norm and reg_norm and prov_norm != reg_norm and prov_norm not in reg_norm and reg_norm not in prov_norm:
            province_conflict = True
    if not region or not matched_delegation_name or province_conflict:
        try:
            # Build the catalog of options. Group delegations under their region.
            options_lines = []
            for r in all_regions:
                r_dlgs = [d for d in all_delegations if d.region_id == r.id]
                if r_dlgs:
                    options_lines.append(f"{r.name}: " + ", ".join(d.name for d in r_dlgs))
                else:
                    options_lines.append(r.name)
            # Only ask Gemini if we have data
            if options_lines:
                prompt = (
                    "Tu es assistant pour matcher une adresse Shopify à notre liste de gouvernorats (régions) et délégations (villes) tunisiens. "
                    "Choisis le couple REGION + VILLE qui correspond le mieux aux champs Shopify. "
                    "Tu DOIS choisir un nom EXACT de la liste fournie. "
                    "IMPORTANT : Les Tunisiens utilisent souvent des transcriptions avec chiffres (3=ع, 5=خ, 7=ح, 9=ق, 2=ء). "
                    "Exemples de transcriptions courantes :\n"
                    "  '9ar9na', 'gargana', 'karkenna' → Kerkennah\n"
                    "  '9afsa', 'gafsa' → Gafsa\n"
                    "  '3in dra7em' → Ain Draham\n"
                    "  'jbenyana', 'jebniana' → Jebeniana\n"
                    "  'nef6a', 'nafta' → Nefta\n"
                    "Si rien ne correspond clairement, réponds 'NONE'. "
                    "Réponds UNIQUEMENT au format : 'REGION: nom | VILLE: nom' ou 'NONE'.\n\n"
                    "Plus d'exemples :\n"
                    "  Champs 'SFAX / jbenyana' → 'REGION: Sfax | VILLE: Jebeniana'\n"
                    "  Champs 'Sfax / 9ar9na ramla' → 'REGION: Sfax | VILLE: Kerkennah'\n"
                    "  Champs 'Tozeur / Nefta' → 'REGION: Tozeur | VILLE: Nefta'\n\n"
                    f"Champs Shopify :\n"
                    f"  Province : {province}\n"
                    f"  City : {city}\n"
                    f"  Address1 : {address1}\n"
                    f"  Address2 : {address2}\n\n"
                    "Liste des régions et délégations :\n"
                    + "\n".join(options_lines)
                    + "\n\nRéponse :"
                )
                ai_response = _gemini_transliterate(prompt)
                try:
                    import logging
                    logging.getLogger(__name__).warning(
                        "Gemini region-match for province=%r city=%r address1=%r: response=%r",
                        province, city, address1, ai_response
                    )
                except Exception:
                    pass
                if ai_response and ai_response.strip().upper() != "NONE":
                    m = re.match(r"REGION:\s*(.+?)\s*\|\s*VILLE:\s*(.+)", ai_response.strip(), re.IGNORECASE)
                    if m:
                        region_name_ai = m.group(1).strip()
                        ville_name_ai = m.group(2).strip()
                        # Find region: try exact (case-insensitive), then fuzzy
                        region_match = None
                        for r in all_regions:
                            if r.name.lower() == region_name_ai.lower():
                                region_match = r
                                break
                        if not region_match:
                            # Fuzzy: closest by Levenshtein
                            best_dist = 999
                            for r in all_regions:
                                d_ = _levenshtein(_normalize(r.name), _normalize(region_name_ai))
                                if d_ < best_dist and d_ <= 3:
                                    region_match = r
                                    best_dist = d_
                        if region_match:
                            region = region_match
                            # Find delegation: exact, then fuzzy within this region
                            ville_match = None
                            for d in all_delegations:
                                if d.region_id == region_match.id and d.name.lower() == ville_name_ai.lower():
                                    ville_match = d
                                    break
                            if not ville_match:
                                # Fuzzy: closest within this region
                                best_dist = 999
                                v_norm = _normalize(ville_name_ai)
                                for d in all_delegations:
                                    if d.region_id != region_match.id:
                                        continue
                                    d_ = _levenshtein(_normalize(d.name), v_norm)
                                    if d_ < best_dist and d_ <= 3:
                                        ville_match = d
                                        best_dist = d_
                            if ville_match:
                                matched_delegation_name = ville_match.name
        except Exception:
            pass

    # If we matched a Delegation by name, replace the free-text city with the
    # canonical Delegation name so the team sees a clean value.
    if matched_delegation_name:
        city = matched_delegation_name
    elif region:
        # No precise delegation matched (customer gave no clean city, or only a
        # street). Avoid dumping messy street text into Ville — fall back to the
        # region name so Ville shows a clean, meaningful value (the full street
        # is still preserved in the address fields). The team can refine the
        # delegation from the dropdown if needed.
        city = region.name

    # 5. Get the Shopify SalesPage (or create it if missing)
    sales_page = SalesPage.objects.filter(name__iexact="Barats.tn").first()
    if not sales_page:
        sales_page = SalesPage.objects.filter(is_active=True).order_by("id").first()
    if not sales_page:
        log_action(
            None, AuditLog.OTHER,
            description=f"Webhook Shopify #{shopify_order_number} : aucune SalesPage trouvée, commande ignorée.",
        )
        return JsonResponse({"status": "error", "message": "Aucune SalesPage configurée."}, status=500)

    # 6. Check for duplicate (Shopify retries webhooks if no 200 is returned)
    if shopify_order_id:
        existing = Order.objects.filter(
            notes__contains=f"shopify_order_id={shopify_order_id}"
        ).first()
        if existing:
            return JsonResponse({"status": "ok", "message": "Already processed", "order_id": existing.id})

    # 7. Get-or-create the Customer
    customer, _ = Customer.objects.get_or_create(phone=phone_norm, defaults={"name": name})
    if name and customer.name != name:
        customer.name = name
        customer.save(update_fields=["name"])

    # 8. Create the Order draft
    line_items = payload.get("line_items") or []
    notes_parts = [
        f"shopify_order_id={shopify_order_id}",
        f"shopify_order_number={shopify_order_number}",
    ]
    customer_note = payload.get("note") or ""
    if customer_note:
        notes_parts.append(f"note_client: {customer_note}")

    from decimal import Decimal
    delivery_fee = Decimal("7")
    try:
        total_shipping = sum(
            Decimal(str(s.get("price") or "0"))
            for s in (payload.get("shipping_lines") or [])
        )
        if total_shipping > 0:
            delivery_fee = total_shipping
    except Exception:
        pass

    with transaction.atomic():
        order = Order.objects.create(
            customer=customer,
            sales_page=sales_page,
            region=region,
            ville=city,
            localite="",
            address=address,
            delivery_fee=delivery_fee,
            discount=Decimal("0"),
            notes=" | ".join(notes_parts),
            status=Order.NON_CONFIRMEE,
            created_by=None,  # No user — webhook is unauthenticated
        )

    # Helper: extract size from a Shopify line item by looking at variant_title
    # and line_item properties. Returns the matched size string or "".
    def _extract_variant(li, product, strict=False):
        """Pick the right ProductVariant from a Shopify line_item based on the
        color in variant_title. Falls back to the first variant if no match,
        UNLESS strict=True (used for bundles where a wrong fallback would create
        a fake order line with the wrong color).

        Searches across the whole product family (parent + V2/V3 children) so
        even if the parent has 'Gray' but the V2 sub-product doesn't, we still
        find it.
        """
        if not product:
            return None

        # Build family: root + all descendants (mirrors _extract_size).
        root = product
        while getattr(root, "parent_product", None):
            root = root.parent_product
        family = [root]
        try:
            from .models import Product as _Product
            for child in _Product.objects.filter(parent_product=root):
                family.append(child)
                for grand in _Product.objects.filter(parent_product=child):
                    family.append(grand)
        except Exception:
            pass

        # Collect all variants across the family. Track which variant belongs
        # to the specifically-requested product (preferred) vs family fallback.
        all_variants_self = list(product.variants.all())
        all_variants_family = []
        for p in family:
            for v in p.variants.all():
                all_variants_family.append(v)
        if not all_variants_family:
            return None

        # RULE: a product with exactly ONE colour/variant has no ambiguity —
        # whether or not Shopify sent a colour. Always use that single variant.
        # (Shopify omits the colour for single-colour products, e.g. Icy Maze
        # whose variant_title is just "S".)
        if len(all_variants_self) == 1:
            return all_variants_self[0]
        if len(all_variants_family) == 1:
            return all_variants_family[0]

        # Gather color candidates from variant_title and options
        candidates = []
        v_title = (li.get("variant_title") or "").strip()
        if v_title:
            for part in re.split(r"[/|\\,;]| - ", v_title):
                p = part.strip()
                if p:
                    candidates.append(p)
        for k in ("option1", "option2", "option3"):
            ov = li.get(k) or ""
            if ov:
                candidates.append(str(ov).strip())
        for prop in (li.get("properties") or []):
            pname = (prop.get("name") or "").strip().lower()
            pval = (prop.get("value") or "").strip()
            if pval and any(k in pname for k in ("color", "couleur", "colour")):
                candidates.append(pval)

        if not candidates:
            return None if strict else (all_variants_self[0] if all_variants_self else all_variants_family[0])

        # Build a list of FR/EN color synonyms so "Bleu" matches "Blue"/"BLUE",
        # "Noir" matches "Black"/"BLACK", "Gris" matches "Gray"/"Grey", etc.
        # The list is one-way: if a candidate equals one of the synonyms in a
        # group, we accept any variant whose color equals ANY synonym in the same group.
        color_aliases = [
            {"bleu", "blue", "azul"},
            {"noir", "black", "noire"},
            {"blanc", "white", "blanche"},
            {"rouge", "red", "rojo"},
            {"vert", "green", "verte"},
            {"gris", "gray", "grey", "grise"},
            {"jaune", "yellow"},
            {"orange", "naranja"},
            {"rose", "pink"},
            {"violet", "purple", "violet"},
            {"marron", "brown", "marrone", "café", "cafe"},
            {"beige", "cream", "crème", "creme"},
            {"camo", "camouflage"},
        ]

        def _synonyms_of(word):
            """Return the set of synonyms that contain this word (lowercased)."""
            w = word.strip().lower()
            for group in color_aliases:
                if w in group:
                    return group
            return {w}

        # Try to match each candidate against color_label and color_name.
        # Two passes: first the product's own variants, then the family.
        for variant_pool in (all_variants_self, all_variants_family):
            for cand in candidates:
                cand_syns = _synonyms_of(cand)
                for v in variant_pool:
                    lbl = (v.color_label or "").strip().lower()
                    nm = (v.color_name or "").strip().lower()
                    if lbl in cand_syns or nm in cand_syns:
                        return v
            # Partial contains match (handles "Gris foncé" matches "Gris")
            for cand in candidates:
                cl = cand.lower()
                for v in variant_pool:
                    lbl = (v.color_label or "").strip().lower()
                    nm = (v.color_name or "").strip().lower()
                    if lbl and (lbl in cl or cl in lbl):
                        return v
                    if nm and (nm in cl or cl in nm):
                        return v

        # No color matched any variant of this product (or its family).
        # - In strict mode (bundles): return None so the team picks manually.
        # - In normal mode: fall back to first variant of the product itself.
        if strict:
            return None
        return all_variants_self[0] if all_variants_self else all_variants_family[0]

    def _extract_size(li, product):
        # Shopify variant_title format examples:
        #   "Blanc / XL"
        #   "L / Bleu"
        #   "Taille XL"
        #   "39"  (just the number)
        candidates = []
        v_title = (li.get("variant_title") or "").strip()
        if v_title:
            # Split by common separators
            for part in re.split(r"[/|\\,;]| - ", v_title):
                p = part.strip()
                if p:
                    candidates.append(p)
        # Also look in 'properties' (custom fields the merchant set up)
        for prop in (li.get("properties") or []):
            pname = (prop.get("name") or "").strip().lower()
            pval = (prop.get("value") or "").strip()
            if pval and any(k in pname for k in ("size", "taille", "pointure")):
                candidates.append(pval)
        # Also look at the variant's "option" fields (Shopify product variant)
        # which may come through differently
        for k in ("option1", "option2", "option3"):
            ov = li.get(k) or ""
            if ov:
                candidates.append(str(ov).strip())

        if not candidates:
            return ""

        # Collect known sizes for this product AND its family (parent + V2/V3 children)
        # across all variants. This is important because the merchant may have
        # "Pull WaveLine" (sizes 1-4) and "Pull WaveLine V2" (sizes 2-5) and a
        # customer who picks 2XL should land on size "5" no matter which sub-product matched.
        known_sizes = set()
        if product:
            # Find the root: walk up parent_product until None
            root = product
            while getattr(root, "parent_product", None):
                root = root.parent_product
            # Build family = root + all descendants
            family = [root]
            try:
                from .models import Product as _Product
                # children of root (V2)
                for child in _Product.objects.filter(parent_product=root):
                    family.append(child)
                    # grandchildren (V3)
                    for grand in _Product.objects.filter(parent_product=child):
                        family.append(grand)
            except Exception:
                pass
            for p in family:
                for v in p.variants.all():
                    for u in v.units.all():
                        if u.size:
                            known_sizes.add(u.size.strip())

        # LETTER → NUMBER mapping for Tunisian size convention:
        #   1=S, 2=M, 3=L, 4=XL, 5=XXL
        # Plus a special rule: if size "1" doesn't exist in stock, "S" falls back to "2".
        letter_to_number = {
            "xs": "1", "s": "1",
            "m": "2",
            "l": "3",
            "xl": "4",
            "xxl": "5", "2xl": "5",
            "xxxl": "5", "3xl": "5",  # treat as XXL since we only go up to 5
        }
        # Reverse direction (rarely used here but available)
        # number_to_letter = {"1": "S", "2": "M", "3": "L", "4": "XL", "5": "XXL"}

        if known_sizes:
            # 1. Direct match (case insensitive) — handles cases where the
            # product genuinely uses S/M/L sizes
            for cand in candidates:
                for ks in known_sizes:
                    if cand.lower() == ks.lower():
                        return ks  # return the canonical capitalization

            # 2. Letter → number conversion (the common Tunisian case)
            for cand in candidates:
                num = letter_to_number.get(cand.lower())
                if num is None:
                    continue
                if num in known_sizes:
                    return num
                # SPECIAL RULE: if S (= "1") is not stocked, fall back to M ("2")
                if num == "1" and "2" in known_sizes:
                    return "2"

            # 3. Number → letter conversion (less common but possible)
            number_to_letter = {"1": "S", "2": "M", "3": "L", "4": "XL", "5": "XXL"}
            for cand in candidates:
                ltr = number_to_letter.get(cand.strip())
                if ltr and ltr in known_sizes:
                    return ltr

        # 4. No known sizes or no match — fallback: regex-look-like-a-size
        for cand in candidates:
            if re.match(r"^(xs|s|m|l|xl|xxl|xxxl|3xl|4xl|\d{1,2})$", cand.lower()):
                return cand
        return ""

    import re

    # 9. Map each line_item: first try matching to an Offer (bundle), then to a Product.
    # If we can't find one, we still record the line as a note for the team.
    from .models import Offer, OfferProduct, OrderOffer
    unmatched_items = []
    for li in line_items:
        title = (li.get("title") or li.get("name") or "").strip()
        variant_title = (li.get("variant_title") or "").strip()
        quantity = int(li.get("quantity") or 1)
        unit_price = Decimal(str(li.get("price") or "0"))

        if not title:
            continue

        title_lower = title.lower()

        # --- (A) Try to match an OFFER (bundle) by name first ---
        offer = Offer.objects.filter(name__iexact=title, is_active=True).first()
        if not offer:
            # Try "offer name contained in shopify title"
            candidates = []
            for o in Offer.objects.filter(is_active=True):
                o_name_lower = (o.name or "").strip().lower()
                if o_name_lower and o_name_lower in title_lower:
                    candidates.append(o)
            if candidates:
                # Prefer longest match
                offer = max(candidates, key=lambda o: len(o.name))

        # --- (B) ALSO try to match a PRODUCT (so we have both candidates) ---
        product = Product.objects.filter(name__iexact=title).first()
        if not product:
            candidates = []
            for p in Product.objects.all():
                p_name_lower = (p.name or "").strip().lower()
                if p_name_lower and p_name_lower in title_lower:
                    candidates.append(p)
            if candidates:
                product = max(candidates, key=lambda p: len(p.name))
        if not product and variant_title:
            full_title = f"{title} {variant_title}".lower()
            for p in Product.objects.all():
                p_name_lower = (p.name or "").strip().lower()
                if p_name_lower and p_name_lower in full_title:
                    product = p
                    break

        # --- (C) Ask Gemini to validate the best choice between offer / product / nothing ---
        # This handles cases like Shopify title "Pull Polo Crochet Blueline" where
        # the classic substring match might wrongly pick an Offer named "Pull Polo".
        # Only invoke when at least one candidate was found OR no clear exact match.
        gemini_pick = None
        try:
            all_offers = list(Offer.objects.filter(is_active=True))
            all_products = list(Product.objects.all())
            options_lines = []
            for o in all_offers:
                if o.name:
                    options_lines.append(f"OFFRE: {o.name}")
            for p in all_products:
                if p.name:
                    options_lines.append(f"PRODUIT: {p.name}")
            if options_lines and (offer or product):
                # Only ask Gemini when we have at least one classic candidate AND
                # the match isn't a clean exact match on the title.
                exact_offer_match = bool(Offer.objects.filter(name__iexact=title, is_active=True).first())
                exact_product_match = bool(Product.objects.filter(name__iexact=title).first())
                if not (exact_offer_match or exact_product_match):
                    prompt = (
                        "Tu es assistant pour matcher un titre de commande Shopify à notre catalogue.\n"
                        "Notre catalogue a deux types : OFFRE (pack/ensemble de produits) et PRODUIT (article seul).\n\n"
                        "RÈGLE PRIMORDIALE : le PREMIER MOT du titre Shopify détermine le type d'article.\n"
                        "  - Titre commence par 'Pull' → cherche 'Pull X' dans le catalogue (OFFRE ou PRODUIT)\n"
                        "  - Titre commence par 'Polo' → cherche 'Polo X'\n"
                        "  - Titre commence par 'Pants' → cherche 'Pants X'\n"
                        "  - Titre commence par 'Veste' → cherche 'Veste X'\n"
                        "  - Titre commence par 'Ensemble' ou 'Tenue' → cherche 'Ensemble X' ou 'Tenue X'\n"
                        "JAMAIS un 'Pull X' ne doit matcher un 'Ensemble X' ou inversement.\n\n"
                        "Si rien ne correspond clairement, réponds 'NONE'.\n"
                        "Réponds UNIQUEMENT par : 'OFFRE: nom' ou 'PRODUIT: nom' ou 'NONE'.\n\n"
                        "Exemples critiques :\n"
                        "  'Pull Camo ZR' → 'OFFRE: Pull Camo' (PAS 'Ensemble Camo ZR' — c'est un Pull seul, pas un ensemble)\n"
                        "  'Pull Polo Crochet Blueline' → 'OFFRE: Pull BlueLine' (PAS 'Ensemble Blueline')\n"
                        "  'Ensemble Camo ZR' → 'OFFRE: Ensemble Camo ZR' (commence par Ensemble)\n"
                        "  'Pull Vintage' → 'PRODUIT: PULL VINTAGE'\n\n"
                        f"Titre Shopify : {title}\n"
                        f"Variante : {variant_title or '(aucune)'}\n\n"
                        "Catalogue :\n"
                        + "\n".join(options_lines)
                        + "\n\nRéponse :"
                    )
                    ai_response = _gemini_transliterate(prompt)
                    # Log what Gemini returned for debugging
                    try:
                        import logging
                        logging.getLogger(__name__).warning(
                            "Gemini product-match for title=%r: response=%r",
                            title, ai_response
                        )
                    except Exception:
                        pass
                    if ai_response:
                        ai_response = ai_response.strip().strip('"').strip("'")
                        # Tolerate response without prefix (e.g. just "Pull Polo")
                        # Try OFFRE/PRODUIT prefix first, then fall back to fuzzy lookup
                        upper = ai_response.upper()
                        if upper == "NONE":
                            offer = None
                            product = None
                        elif upper.startswith("OFFRE:"):
                            target_name = ai_response[6:].strip()
                            for o in all_offers:
                                if o.name and o.name.strip().lower() == target_name.lower():
                                    offer = o
                                    product = None
                                    gemini_pick = "offer"
                                    break
                        elif upper.startswith("PRODUIT:"):
                            target_name = ai_response[8:].strip()
                            for p in all_products:
                                if p.name and p.name.strip().lower() == target_name.lower():
                                    product = p
                                    offer = None
                                    gemini_pick = "product"
                                    break
                        else:
                            # Gemini answered without prefix — search the name in both lists
                            name_lower = ai_response.lower()
                            found = False
                            for p in all_products:
                                if p.name and p.name.strip().lower() == name_lower:
                                    product = p
                                    offer = None
                                    gemini_pick = "product_no_prefix"
                                    found = True
                                    break
                            if not found:
                                for o in all_offers:
                                    if o.name and o.name.strip().lower() == name_lower:
                                        offer = o
                                        product = None
                                        gemini_pick = "offer_no_prefix"
                                        break
        except Exception:
            # If Gemini fails for any reason, fall back to the classic match
            pass

        # Safety guard: if Gemini picked an OFFRE/PRODUIT whose first word
        # mismatches the Shopify title's first word (e.g. 'Pull Camo ZR' →
        # 'Ensemble Camo ZR'), reject Gemini's pick to avoid wrong matching.
        # Common boundary: 'Pull'/'Polo'/'Pants'/'Veste'/'Shirt'/'Short' should NOT
        # become 'Ensemble' or 'Tenue', and vice versa.
        try:
            single_keywords = {"pull", "polo", "pants", "pant", "veste", "shirt", "short", "doudoune", "gillet", "5 pieces"}
            bundle_keywords = {"ensemble", "tenue"}
            title_first = title.strip().lower().split()[0] if title.strip() else ""
            picked_obj = offer or product
            picked_name = (picked_obj.name if picked_obj else "")
            picked_first = picked_name.strip().lower().split()[0] if picked_name else ""
            title_is_single = title_first in single_keywords
            title_is_bundle = title_first in bundle_keywords
            picked_is_single = picked_first in single_keywords
            picked_is_bundle = picked_first in bundle_keywords
            if (title_is_single and picked_is_bundle) or (title_is_bundle and picked_is_single):
                # Reject Gemini pick — it crossed the boundary
                try:
                    import logging
                    logging.getLogger(__name__).warning(
                        "Gemini cross-boundary pick REJECTED: title=%r → pick=%r (type mismatch)",
                        title, picked_name
                    )
                except Exception:
                    pass
                # Try to find a same-type alternative
                title_words = set(title.lower().split())
                alt_found = False
                if title_is_single:
                    # Score each candidate by how many words from the title overlap
                    best_score = 0
                    best_offer = None
                    best_product = None
                    for o in all_offers:
                        if not o.name:
                            continue
                        o_first = o.name.strip().lower().split()[0]
                        if o_first not in single_keywords:
                            continue
                        # Word overlap (excluding common words)
                        o_words = set(o.name.lower().split())
                        score = len(o_words & title_words)
                        if score > best_score:
                            best_score = score
                            best_offer = o
                            best_product = None
                    for p in all_products:
                        if not p.name:
                            continue
                        p_first = p.name.strip().lower().split()[0]
                        if p_first not in single_keywords:
                            continue
                        p_words = set(p.name.lower().split())
                        score = len(p_words & title_words)
                        if score > best_score:
                            best_score = score
                            best_product = p
                            best_offer = None
                    if best_offer:
                        offer = best_offer
                        product = None
                        gemini_pick = "guard_fix_offer"
                        alt_found = True
                    elif best_product:
                        product = best_product
                        offer = None
                        gemini_pick = "guard_fix_product"
                        alt_found = True
                if not alt_found:
                    # No safer alternative — drop the bad pick
                    offer = None
                    product = None
        except Exception:
            pass

        # --- (D) Process the final pick ---
        if offer:
            # Found a bundle — create OrderOffer + child OrderLines (one per product in offer).
            # The merchant uses Shopify variant titles like "Gris/Blanc" where
            # each part (split on "/") corresponds, IN ORDER, to a sub-product
            # of the offer. So we split the variant title and pass each part
            # to the matching sub-product.
            order_offer = OrderOffer.objects.create(
                order=order, offer=offer,
                offer_name=offer.name,
                bundle_price=offer.bundle_price,
                quantity=quantity,
            )
            # Split variant_title by "/" to get one color (or size) per slot.
            # We do NOT assume order matches our sub-products order. Instead, for
            # each sub-product, we try ALL color candidates in the title and
            # pick the one that actually matches one of its variants. If a
            # candidate is "claimed" by one sub-product, the others can still
            # use it (e.g. two sub-products with the same Gray variant).
            v_title_full = (li.get("variant_title") or "").strip()
            parts = [p.strip() for p in v_title_full.split("/") if p.strip()]
            offer_products = list(offer.products.all())
            # Heuristic to separate the size token from color tokens.
            # A size token is short and matches "S/M/L/XL/XXL/2XL/3XL" or just digits.
            import re as _re_local
            def _looks_like_size(tok):
                t = (tok or "").strip().lower()
                if not t:
                    return False
                return bool(_re_local.match(r"^(xs|s|m|l|xl|xxl|xxxl|2xl|3xl|4xl|\d{1,2})$", t))
            color_parts = [p for p in parts if not _looks_like_size(p)]
            size_parts  = [p for p in parts if _looks_like_size(p)]
            shared_size_hint = size_parts[-1] if size_parts else ""

            # Track which color candidates have been "claimed" so we don't
            # assign the same color to two sub-products if there's a better match.
            # First pass: prefer assigning each sub-product a color UNIQUE to it.
            # Fallback pass: if a sub-product has no unique match, allow shared.
            assignments = [None] * len(offer_products)  # color string for each sub
            for i, op in enumerate(offer_products):
                # Find which of color_parts matches a variant of this sub-product
                synthetic_li_test = dict(li)
                for cand in color_parts:
                    synthetic_li_test["variant_title"] = cand
                    match = _extract_variant(synthetic_li_test, op.product, strict=True)
                    if match is not None:
                        assignments[i] = cand
                        break

            for i, op in enumerate(offer_products):
                color_for_this = assignments[i] or ""
                synthetic_title = (color_for_this + "/" + shared_size_hint) if shared_size_hint else color_for_this
                synthetic_li = dict(li)
                synthetic_li["variant_title"] = synthetic_title

                size_guess = _extract_size(synthetic_li, op.product)
                variant_guess = _extract_variant(synthetic_li, op.product, strict=True)
                OrderLine.objects.create(
                    order=order,
                    order_offer=order_offer,
                    product=op.product,
                    variant=variant_guess,
                    size=size_guess,
                    quantity=op.quantity * quantity,
                    unit_price=0,
                )
            continue  # Done with this line_item

        # --- (E) Process as simple product ---
        if not product:
            unmatched_items.append(f"{title} (qté {quantity})")
            continue

        variant = _extract_variant(li, product)
        size_guess = _extract_size(li, product)
        OrderLine.objects.create(
            order=order,
            product=product,
            variant=variant,
            size=size_guess,
            quantity=quantity,
            unit_price=unit_price,
        )

    # End of `for li in line_items:` loop

    # 10. If there are unmatched items, add them to the notes
    if unmatched_items:
        order.notes += " | ARTICLES NON RECONNUS: " + "; ".join(unmatched_items)
        order.save(update_fields=["notes"])

    # 11. Recompute total — refresh from DB first to bust any cached
    # relations from earlier (otherwise the freshly-created OrderOffer might
    # not appear in self.order_offers.all()).
    order.refresh_from_db()
    order.recalc_total()

    # 12. Audit log — put line_items first so they're not truncated.
    audit_extra = "LINE_ITEMS=" + str(line_items) + " | PAYLOAD=" + str(payload)
    log_action(
        None, AuditLog.CREATE,
        description=(
            f"Commande Shopify #{shopify_order_number} reçue → Order #{order.id} créée "
            f"({len(line_items)} ligne(s), {len(unmatched_items)} non reconnue(s))"
        ),
        target_model="Order", target_id=order.id,
        extra=audit_extra[:50000],
    )

    return JsonResponse({
        "status": "ok",
        "order_id": order.id,
        "unmatched": unmatched_items,
    })


# ---- Admin tools page (superuser only) -------------------------------------

@login_required(login_url="/login/")
def api_debug_navex_etat(request):
    """Debug-only: try multiple Navex API parameter variations to find which
    one returns the exchange return barcode. Tries with/without include_echange,
    different parameter formats, GET and POST.
    """
    if not request.user.is_superuser:
        return JsonResponse({"status": "error", "message": "Accès refusé."}, status=403)
    bordereau = (request.GET.get("bordereau") or "").strip()
    if not bordereau:
        return JsonResponse({"status": "error", "message": "Bordereau requis."}, status=400)
    import urllib.request, urllib.parse
    token = os.environ.get("NAVEX_API_TOKEN", "")
    if not token:
        return JsonResponse({"status": "error", "message": "Token manquant."}, status=500)

    url = f"https://app.navex.tn/api/rashop-etat-{token}/v1/post.php"

    variations = [
        ("POST code=X (singular) + include_echange=1", {"code": bordereau, "include_echange": "1"}, "POST"),
        ("POST code=X (singular) without include", {"code": bordereau}, "POST"),
        ("POST codes=X (plural) + include_echange=1", {"codes": bordereau, "include_echange": "1"}, "POST"),
        ("POST codes=X (plural) without include", {"codes": bordereau}, "POST"),
        ("POST code=X + include-echange=1 (hyphen)", {"code": bordereau, "include-echange": "1"}, "POST"),
        ("GET code=X + include_echange=1", {"code": bordereau, "include_echange": "1"}, "GET"),
    ]

    results = []
    for label, params, method in variations:
        try:
            if method == "POST":
                body = urllib.parse.urlencode(params).encode("utf-8")
                req = urllib.request.Request(url, data=body, method="POST")
                req.add_header("Content-Type", "application/x-www-form-urlencoded")
            else:
                full_url = url + "?" + urllib.parse.urlencode(params)
                req = urllib.request.Request(full_url, method="GET")
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    data = {"_not_json": raw[:500]}
        except Exception as e:
            data = {"_error": str(e)}
        results.append({"variation": label, "params": params, "method": method, "response": data})

    return JsonResponse({
        "status": "ok",
        "bordereau": bordereau,
        "results": results,
    }, json_dumps_params={"indent": 2, "ensure_ascii": False})


@login_required(login_url="/login/")
def admin_tools(request):
    """Maintenance/repair page with one-click action buttons.
    Only accessible to superusers."""
    if not request.user.is_superuser:
        return redirect("home")
    return render(request, "inventory/admin_tools.html", {})


@csrf_exempt
@require_POST
@login_required(login_url="/login/")
def api_admin_run_tool(request, tool_name):
    """Run a maintenance tool by name. Returns the captured output as JSON."""
    if not request.user.is_superuser:
        return JsonResponse({"status": "error", "message": "Accès refusé."}, status=403)
    import io
    from django.core.management import call_command

    # Whitelist of allowed tools — never call arbitrary commands
    ALLOWED = {
        "fix_supprime_navex_orders_dryrun": ("fix_supprime_navex_orders", []),
        "fix_supprime_navex_orders_apply":  ("fix_supprime_navex_orders", ["--apply"]),
        "fix_livree_orders_dryrun":         ("fix_livree_orders", []),
        "fix_livree_orders_apply":          ("fix_livree_orders", ["--apply"]),
        "recalc_order_totals":              ("recalc_order_totals", []),
    }
    if tool_name not in ALLOWED:
        return JsonResponse({"status": "error", "message": f"Outil inconnu : {tool_name}"}, status=400)
    cmd, cmd_args = ALLOWED[tool_name]
    out = io.StringIO()
    err = io.StringIO()
    try:
        call_command(cmd, *cmd_args, stdout=out, stderr=err)
        return JsonResponse({
            "status": "ok",
            "tool": tool_name,
            "output": out.getvalue(),
            "errors": err.getvalue(),
        })
    except Exception as e:
        return JsonResponse({
            "status": "error",
            "message": str(e),
            "output": out.getvalue(),
            "errors": err.getvalue(),
        }, status=500)


# ---- Regions / Delegations (cascaded dropdown) -----------------------------

@login_required(login_url="/login/")
def api_region_delegations(request, region_id):
    """Return the list of delegations (sub-zones) for a given governorate.
    Used by the frontend to populate a cascaded dropdown."""
    from .models import Region, Delegation
    if not _orders_role_check(request):
        return JsonResponse({"status": "error"}, status=403)
    try:
        region = Region.objects.get(pk=region_id)
    except Region.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Gouvernorat introuvable."}, status=404)
    delegations = Delegation.objects.filter(region=region, is_active=True).order_by("name")
    return JsonResponse({
        "status": "ok",
        "region": {"id": region.id, "name": region.name},
        "delegations": [{"id": d.id, "name": d.name} for d in delegations],
    })


@login_required(login_url="/login/")
def api_all_delegations(request):
    """Return ALL regions and their delegations in a single payload.
    Used by the unified searchable dropdown.
    """
    from .models import Region, Delegation
    if not _orders_role_check(request):
        return JsonResponse({"status": "error"}, status=403)
    regions = Region.objects.filter(is_active=True).order_by("name").prefetch_related("delegations")
    out = []
    for r in regions:
        out.append({
            "id": r.id,
            "name": r.name,
            "delegations": [
                {"id": d.id, "name": d.name}
                for d in r.delegations.filter(is_active=True).order_by("name")
            ],
        })
    return JsonResponse({"status": "ok", "regions": out})


# ---- User theme preference (light/dark mode) -------------------------------

@csrf_exempt
@require_POST
@login_required(login_url="/login/")
def api_user_theme(request):
    """Update the logged-in user's theme preference (dark/light)."""
    from .models import UserProfile
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"status": "error"}, status=400)
    theme = (data.get("theme") or "").strip().lower()
    if theme not in (UserProfile.THEME_DARK, UserProfile.THEME_LIGHT):
        return JsonResponse({"status": "error", "message": "Theme invalide."}, status=400)
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    profile.theme = theme
    profile.save(update_fields=["theme"])
    return JsonResponse({"status": "ok", "theme": theme})


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
def api_order_set_note(request, pk):
    """Save (or clear) the sticky note on an order. Used by the note icon in
    the orders list. Reuses the `status_note` field."""
    if not _orders_role_check(request):
        return JsonResponse({"status": "error", "message": "Accès refusé."}, status=403)
    from .models import Order, log_action, AuditLog
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "JSON invalide."}, status=400)
    order = get_object_or_404(Order, pk=pk)
    note = (data.get("note") or "").strip()[:300]
    order.status_note = note
    order.save(update_fields=["status_note", "updated_at"])
    log_action(
        request.user, AuditLog.EDIT,
        description=f"Commande #{order.id} : note mise à jour" + (f" → {note}" if note else " (effacée)"),
        request=request, target_model="Order", target_id=order.id,
    )
    return JsonResponse({"status": "ok", "note": note})


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

    # ---- Cancellation is only allowed from CONFIRMEE (or pre-Navex states). ----
    # Once an order is en route / at depot / returning / returned / delivered /
    # paid, it can no longer be cancelled — it's out of our hands at Navex.
    if new_status == Order.ANNULEE:
        cancellable_from = (
            Order.NON_CONFIRMEE, Order.CONFIRMEE, Order.RAPPELER,
            Order.INJOIGNABLE, Order.PAS_SERIEUX,
        )
        if old_status not in cancellable_from:
            return JsonResponse({
                "status": "error",
                "message": f"Impossible d'annuler une commande au statut « {old_label} ». "
                           f"Seules les commandes confirmées (ou non encore expédiées) peuvent être annulées.",
                "code": "NOT_CANCELLABLE",
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

        # If the order came from Shopify, also cancel it on their side so it
        # doesn't stay "open" in their admin.
        shopify_cancelled = False
        shopify_id = _extract_shopify_order_id_from_notes(order.notes)
        if shopify_id:
            ok_sh, sh_resp = _shopify_cancel_order(shopify_id)
            if ok_sh:
                shopify_cancelled = True
                log_action(
                    request.user, AuditLog.OTHER,
                    description=f"Shopify : commande {shopify_id} annulée (Order #{order.id})",
                    request=request, target_model="Order", target_id=order.id,
                    extra=str(sh_resp)[:5000],
                )
            else:
                # Don't block — just log. The team can fix on Shopify manually.
                log_action(
                    request.user, AuditLog.OTHER,
                    description=f"Shopify : ÉCHEC d'annulation pour {shopify_id} (Order #{order.id}) — à fixer manuellement",
                    request=request, target_model="Order", target_id=order.id,
                    extra=str(sh_resp)[:5000],
                )

        log_action(
            request.user, AuditLog.STATUS_CHANGE,
            description=(
                f"Commande #{order.id} annulée : "
                f"{dict(Order.CANCEL_REASON_CHOICES).get(cancel_reason, cancel_reason)}"
                + (" (bordereau Navex également supprimé)" if navex_was_cancelled else "")
                + (" (Shopify également annulé)" if shopify_cancelled else "")
            ),
            request=request,
            target_model="Order", target_id=order.id,
        )
        return JsonResponse({
            "status": "ok", "new_status": Order.ANNULEE,
            "label": valid[Order.ANNULEE],
            "cancel_reason": cancel_reason,
            "navex_was_cancelled": navex_was_cancelled,
            "shopify_was_cancelled": shopify_cancelled,
        })

    # ---- Other simple transitions (injoignable, pas_serieux, rappeler_plus_tard) ----
    # Valid transitions table
    allowed_transitions = {
        Order.NON_CONFIRMEE: [Order.INJOIGNABLE, Order.PAS_SERIEUX, Order.RAPPELER, Order.ANNULEE],
        Order.RAPPELER:      [Order.NON_CONFIRMEE, Order.INJOIGNABLE, Order.PAS_SERIEUX, Order.ANNULEE],
        Order.INJOIGNABLE:   [Order.NON_CONFIRMEE, Order.RAPPELER, Order.PAS_SERIEUX, Order.ANNULEE],
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

    # These statuses require a note explaining the reason.
    note_required = (Order.INJOIGNABLE, Order.RAPPELER, Order.PAS_SERIEUX)
    status_note = (data.get("status_note") or "").strip()
    if new_status in note_required and not status_note:
        return JsonResponse({
            "status": "error",
            "message": "Une note est obligatoire pour ce statut.",
            "code": "NOTE_REQUIRED",
        }, status=400)

    order.status = new_status
    update_fields = ["status", "updated_at"]
    if status_note:
        order.status_note = status_note[:300]
        update_fields.append("status_note")
    order.save(update_fields=update_fields)
    log_action(
        request.user, AuditLog.STATUS_CHANGE,
        description=f"Commande #{order.id} : {old_label} → {valid[new_status]}"
                    + (f" — note: {status_note}" if status_note else ""),
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
    if not (order.address or "").strip():
        return JsonResponse({"status": "error", "message": "Adresse manquante."}, status=400)

    # Defensive: recompute total right before push, in case it was never updated.
    # Without this, a draft created via autosave but never recalculated would push prix=0.
    order.recalc_total()
    order.refresh_from_db()
    # For NORMAL orders, refuse if total=0 (probably no articles selected).
    # For EXCHANGE orders, total can legitimately be 0 (notre faute → free reshipping)
    # so we don't apply this check.
    if not order.exchange_of_id:
        if not order.total or order.total <= 0:
            return JsonResponse({
                "status": "error",
                "message": "Prix de la commande est 0. Vérifiez que des articles sont bien sélectionnés.",
            }, status=400)

    # If this is an exchange, require that return items have been selected
    if order.exchange_of_id and not order.return_items.exists():
        return JsonResponse({
            "status": "error",
            "message": "Cette commande est un échange mais aucun article retourné n'a été sélectionné. Cliquez sur le bouton '🔄 Retour' pour choisir les articles.",
        }, status=400)

    designation = _build_designation(order)
    nb_article = _count_articles(order)

    # If this is an exchange order, build the exchange-specific params.
    # - echange     : ID of the original delivered order (for our tracking)
    # - article     : (empty, Navex doesn't need a description of returned items)
    # - nb_echange  : count of items being returned
    # - ouvrir      : "Oui" — the client can open and verify the new colis
    # PRICE for exchange: only the delivery fee (default 7 DT) minus discount.
    # The articles' value is NOT charged again — client already paid for those
    # on the original delivered order.
    exchange_str = ""
    article_str = ""
    nb_echange_str = ""
    ouvrir_str = "Oui"  # clients are allowed to open & verify the colis before paying
    exchange_price = None  # if set, overrides the normal order.total
    if order.exchange_of_id:
        exchange_str = str(order.exchange_of_id)
        nb_returns = order.return_items.count()
        nb_echange_str = str(nb_returns) if nb_returns else ""
        ouvrir_str = "Oui"
        # For exchanges: only delivery_fee - discount.
        # If our fault, the team sets discount=delivery_fee → 0 DT.
        # If client takes a more expensive product, team raises delivery_fee.
        from decimal import Decimal
        exchange_price = max(
            Decimal("0"),
            (order.delivery_fee or Decimal("0")) - (order.discount or Decimal("0"))
        )

    prix_str = f"{exchange_price:.0f}" if exchange_price is not None else (f"{order.total:.0f}" if order.total else "0")

    # Standard message appended to every Navex order's "msg" field.
    # Informs the client about exchange policy: 2 days max, no refunds, only exchanges.
    POLICY_MSG = (
        "Les échanges sont acceptés uniquement dans un délai maximum de 2 jours "
        "après réception du colis (échange uniquement, pas de remboursement). "
        "Pour toute demande d'échange, veuillez nous contacter immédiatement au 26200219."
    )
    user_notes = (order.notes or "").strip()
    if user_notes:
        msg_str = f"{user_notes} | {POLICY_MSG}"
    else:
        msg_str = POLICY_MSG

    payload = {
        "prix":           prix_str,
        "nom":            order.customer.name or order.customer.phone,
        "gouvernerat":    order.region.name,
        "ville":          order.ville or "",
        "adresse":        (order.address or order.localite or "").strip() or order.ville or "",
        "tel":            order.customer.phone,
        "tel2":           order.customer.phone2 or "",
        "designation":    designation[:500],
        "nb_article":     str(nb_article),
        "msg":            msg_str[:500],
        "echange":        exchange_str,
        "article":        article_str,
        "nb_echange":     nb_echange_str,
        "ouvrir":         ouvrir_str,
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

    is_exchange_msg = f" [ÉCHANGE de #{order.exchange_of_id}, {order.return_items.count()} article(s) retour]" if order.exchange_of_id else ""
    log_action(
        request.user, AuditLog.NAVEX_PUSH,
        description=f"Commande #{order.id} envoyée à Navex{is_exchange_msg}" + (f" — bordereau {bordereau}" if bordereau else " (bordereau manquant)"),
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
    """Build the Navex 'designation' field in the format the customer expects:

        "94009 Converti | polo ralph kaja summer bleu (M), polo ralph kaja summer beige (M)"

    Breakdown:
        - "94009"     → our order.id
        - "Converti"  → name of the Facebook SalesPage attached to the order
        - " | "       → separator
        - products    → "{product_name} {color} ({size})", repeated once per unit
                        (e.g. quantity=2 → product listed twice, no "2x" prefix)
    """
    units = []  # one string per physical unit being shipped

    def render_unit(line):
        seg = line.product.name
        if line.variant:
            seg += f" {line.variant.color_label or line.variant.color_name}"
        if line.size:
            seg += f" ({line.size})"
        return seg

    # Lines that are part of an offer (multiply quantity by the offer's quantity)
    for oo in order.order_offers.all():
        offer_mult = max(oo.quantity, 1)
        for line in oo.lines.all():
            effective_qty = line.quantity * offer_mult
            unit_str = render_unit(line)
            for _ in range(effective_qty):
                units.append(unit_str)

    # Standalone lines (not part of any offer)
    for line in order.lines.filter(order_offer__isnull=True):
        unit_str = render_unit(line)
        for _ in range(max(line.quantity, 1)):
            units.append(unit_str)

    products_str = ", ".join(units) if units else "Commande"
    page_name = order.sales_page.name if order.sales_page else ""
    prefix = f"{order.id} {page_name}".strip()
    return f"{prefix} | {products_str}" if prefix else products_str


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

    Uses `codes=` (plural, comma-separated) + `include_echange=1` to also
    get the exchange return barcode for orders that are exchanges.
    """
    import urllib.request, urllib.parse
    token = os.environ.get("NAVEX_API_TOKEN", "")
    if not token or not bordereaux:
        return False, {}, {"_error": "missing token or bordereaux"}

    url = f"https://app.navex.tn/api/rashop-etat-{token}/v1/post.php"
    bordereaux = [b for b in bordereaux if b]
    if not bordereaux:
        return False, {}, {"_error": "no valid bordereau"}

    codes_string = ", ".join(bordereaux)
    payload_dict = {
        "codes": codes_string,
        "include_echange": "1",  # Navex returns code_echange + date_echange for exchanges
    }

    try:
        body = urllib.parse.urlencode(payload_dict).encode("utf-8")
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
                # Navex returns "code_echange" + "date_echange" for exchange orders
                # when include_echange=1 is in the payload.
                code_echange = str(entry.get("code_echange") or "").strip()
                date_echange = str(entry.get("date_echange") or "").strip()
                items[code] = {
                    "etat":           str(entry.get("etat") or "").strip(),
                    "motif":          str(entry.get("motif") or "").strip(),
                    "pre_etat":       str(entry.get("pre_etat") or "").strip(),
                    "livreur":        str(entry.get("livreur") or "").strip(),
                    "livreur_tel":    str(entry.get("livreur_tel") or "").strip(),
                    "found":          bool(entry.get("status") == 1),
                    "code_echange":   code_echange,
                    "date_echange":   date_echange,
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
    update_fields = [
        "navex_last_status", "navex_motif", "navex_pre_etat",
        "navex_livreur", "navex_livreur_tel",
        "navex_last_status_raw", "navex_last_synced_at", "updated_at",
    ]
    # If Navex returned a code_echange for this order (it's an exchange),
    # store it as the return barcode.
    code_echange = parsed.get("code_echange") or ""
    if code_echange and code_echange != order.navex_return_barcode:
        order.navex_return_barcode = code_echange[:80]
        update_fields.append("navex_return_barcode")
    order.save(update_fields=update_fields)
    return True


def _sync_navex_for_v2_orders(only_pending=True):
    """Bulk-refresh Navex status for v2 orders that have a bordereau.

    Uses Navex's bulk endpoint: ALL orders synced in ONE API call.
    only_pending=True (default): skip orders already in Annulée status.
    Returns (n_attempted, n_updated).
    """
    from .models import Order, log_action, AuditLog
    from django.db.models import Q
    from datetime import timedelta
    qs = Order.objects.exclude(bordereau_barcode="")
    if only_pending:
        # Skip annulees and supprime_navex. For LIVREE: keep polling any
        # delivered order that doesn't yet have a navex_return_barcode, so a
        # code_echange generated later by Navex (e.g. client kept part of the
        # order at the door) still gets fetched. Once the return barcode is
        # stored, the order drops out of the sync.
        #
        # Stop condition: a delivered order whose linked v1 ShippingOrder has
        # been paid for more than 24h is considered settled — drop it from the
        # sync even if no return barcode ever came. This keeps the polling set
        # from growing forever with normal deliveries.
        final_states = (Order.ANNULEE, Order.SUPPRIME_NAVEX, Order.RETURNED, Order.PAYEE)
        qs = qs.exclude(status__in=final_states)
        paid_cutoff = timezone.now() - timedelta(hours=24)
        # A LIVREE order is "settled" once any linked paid ShippingOrder was
        # paid more than 24h ago. Such orders are excluded.
        settled_livree = (
            Q(status=Order.LIVREE)
            & Q(shipping_orders__status=ShippingOrder.PAID)
            & Q(shipping_orders__paid_at__lt=paid_cutoff)
        )
        # Keep: anything not LIVREE, OR a LIVREE still missing its return
        # barcode AND not yet settled (paid >24h ago).
        qs = qs.filter(
            ~Q(status=Order.LIVREE)
            | (Q(status=Order.LIVREE) & Q(navex_return_barcode=""))
        ).exclude(settled_livree).distinct()
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
        new_navex_status = (parsed.get("etat") or "")[:80]
        o.navex_last_status   = new_navex_status
        o.navex_motif         = (parsed.get("motif") or "")[:200]
        o.navex_pre_etat      = (parsed.get("pre_etat") or "")[:80]
        o.navex_livreur       = (parsed.get("livreur") or "")[:120]
        o.navex_livreur_tel   = (parsed.get("livreur_tel") or "")[:30]
        o.navex_last_status_raw = raw_str
        o.navex_last_synced_at = now
        update_fields = [
            "navex_last_status", "navex_motif", "navex_pre_etat",
            "navex_livreur", "navex_livreur_tel",
            "navex_last_status_raw", "navex_last_synced_at", "updated_at",
        ]
        # If Navex returned a code_echange for this order (it's an exchange),
        # store it as the return barcode. Only update if it's new/different.
        code_echange = (parsed.get("code_echange") or "").strip()
        if code_echange and code_echange != o.navex_return_barcode:
            o.navex_return_barcode = code_echange[:80]
            update_fields.append("navex_return_barcode")
            log_action(
                None, AuditLog.EDIT,
                description=f"Auto: code d'échange Navex récupéré pour #{o.id} → {code_echange}",
                target_model="Order", target_id=o.id,
            )
        # Detect "Supprime" Navex status — means the colis was deleted on their side
        # after our push. We auto-transition the local order to SUPPRIME_NAVEX status
        # so it surfaces clearly (red badge + dedicated filter).
        # Only do this when our local status is still confirmee (i.e. we hadn't
        # already cancelled/annulled it ourselves).
        if (new_navex_status.strip().lower() in ("supprime", "supprimé", "deleted")
                and o.status == Order.CONFIRMEE):
            o.status = Order.SUPPRIME_NAVEX
            update_fields.append("status")
            log_action(
                None, AuditLog.STATUS_CHANGE,
                description=f"Auto: commande #{o.id} passée en 'Supprimé Navex' (sync a détecté 'Supprime' chez Navex, bordereau {o.bordereau_barcode})",
                target_model="Order", target_id=o.id,
            )
        # Detect "Livré" Navex status → auto-transition to our LIVREE status.
        # Variants: "Livré", "Livré Payé", "Livrer", "Livrer Paye", "Livree".
        # Allowed from any forward in-transit state (Confirmée / En cours /
        # Au magasin) — the colis can be delivered after passing through those.
        navex_lower = new_navex_status.strip().lower()
        if (navex_lower in (
                "livre", "livré", "livree", "livrée",
                "livrer", "livrer paye", "livré payé", "livre paye", "livre payé",
            )
            and o.status in (Order.CONFIRMEE, Order.EN_COURS, Order.AU_MAGASIN)):
            old_label = dict(Order.STATUS_CHOICES).get(o.status, o.status)
            o.status = Order.LIVREE
            if "status" not in update_fields:
                update_fields.append("status")
            log_action(
                None, AuditLog.STATUS_CHANGE,
                description=f"Auto: commande #{o.id} {old_label} → 'Livrée' (Navex etat='{new_navex_status}', bordereau {o.bordereau_barcode})",
                target_model="Order", target_id=o.id,
            )

        # Detect "Au magasin" / "En cours" → set the in-transit status. Allowed
        # from Confirmée or between the two in-transit states themselves
        # (the colis can bounce en_cours <-> au_magasin). Don't disturb final
        # or return states.
        if o.status in (Order.CONFIRMEE, Order.EN_COURS, Order.AU_MAGASIN):
            new_v2_status = None
            if navex_lower in ("au magasin", "au-magasin", "au magasin navex"):
                new_v2_status = Order.AU_MAGASIN
            elif navex_lower in ("en cours", "en-cours", "en cours de livraison"):
                new_v2_status = Order.EN_COURS
            if new_v2_status and new_v2_status != o.status:
                old_label = dict(Order.STATUS_CHOICES).get(o.status, o.status)
                o.status = new_v2_status
                if "status" not in update_fields:
                    update_fields.append("status")
                log_action(
                    None, AuditLog.STATUS_CHANGE,
                    description=f"Auto: commande #{o.id} {old_label} → '{dict(Order.STATUS_CHOICES)[new_v2_status]}' "
                                f"(Navex etat='{new_navex_status}', bordereau {o.bordereau_barcode})",
                    target_model="Order", target_id=o.id,
                )

        # Detect "Retour Expéditeur" / "Rtn client/agence" → move the order into
        # RETURNING ("En retour"). Can come from Confirmée or an in-transit
        # status (en_cours / au_magasin). NOTE: the final RETURNED status is set
        # by physical scan in v1, not from Navex sync.
        if o.status in (Order.CONFIRMEE, Order.EN_COURS, Order.AU_MAGASIN):
            if navex_lower in ("retour expediteur", "retour expéditeur",
                               "retour vers expediteur", "retour vers expéditeur",
                               "rtn client/agence", "rtn client", "rtn agence"):
                old_label = dict(Order.STATUS_CHOICES).get(o.status, o.status)
                o.status = Order.RETURNING
                if "status" not in update_fields:
                    update_fields.append("status")
                log_action(
                    None, AuditLog.STATUS_CHANGE,
                    description=f"Auto: commande #{o.id} {old_label} → 'En retour' "
                                f"(Navex etat='{new_navex_status}', bordereau {o.bordereau_barcode})",
                    target_model="Order", target_id=o.id,
                )

        # Detect "Rtn client/agence" → mark the order's ProductUnits as EARLY_RETURN
        # (the customer refused; the parcel is on its way back to us).
        # Only flip units that are currently SHIPPED — don't touch PAID/RETURNED.
        if navex_lower in ("rtn client/agence", "rtn client", "rtn agence", "retour anticipe", "retour anticipé"):
            _flip_order_units_status(o, ProductUnit.SHIPPED, ProductUnit.EARLY_RETURN, "early_return")

        # Detect "Retour recu" → unit at Navex hub, waiting for our physical pickup.
        # Flip EARLY_RETURN and SHIPPED units → AT_DEPOT.
        if navex_lower in ("retour recu", "retour reçu", "retourne", "retourné", "retour confirme", "retour confirmé"):
            _flip_order_units_status(o, ProductUnit.EARLY_RETURN, ProductUnit.AT_DEPOT, "at_depot")
            # Also catch cases where the SHIPPED→EARLY_RETURN step was skipped
            # (Navex jumped straight to "Retour recu") — flip those too.
            _flip_order_units_status(o, ProductUnit.SHIPPED, ProductUnit.AT_DEPOT, "at_depot")

        # NOTE: confirmed return → RETURNED happens via physical scan in the warehouse,
        # not from Navex sync. Otherwise we'd report "back in stock" before it's actually back.

        o.save(update_fields=update_fields)
        n_updated += 1
    return n_attempted, n_updated


def _flip_order_units_status(order, from_status, to_status, movement_type):
    """For all ProductUnits linked to the v2 Order (via ShippingOrder.order),
    if currently in `from_status`, flip to `to_status` and record a StockMovement.

    Used by the Navex sync to auto-mark units as 'early_return' or 'returned'
    based on the order's Navex status, so the warehouse team sees the correct
    physical state without scanning each unit one by one.
    """
    from .models import log_action, AuditLog, StockMovement
    n_flipped = 0
    for so in order.shipping_orders.all():
        for item in so.items.select_related("unit"):
            unit = item.unit
            if unit and unit.status == from_status:
                unit.status = to_status
                unit.save(update_fields=["status", "updated_at"])
                StockMovement.objects.create(
                    unit=unit,
                    movement_type=movement_type,
                    reference=f"Auto sync Navex — commande v2 #{order.id}",
                )
                n_flipped += 1
    if n_flipped:
        log_action(
            None, AuditLog.STATUS_CHANGE,
            description=(
                f"Auto Navex sync: {n_flipped} unité(s) passées de {from_status} → {to_status} "
                f"pour la commande #{order.id} (bordereau {order.bordereau_barcode})"
            ),
            target_model="Order", target_id=order.id,
        )
    return n_flipped


def _shopify_get_access_token():
    """Exchange the Dev Dashboard Client ID + Client Secret for a short-lived
    Admin API access token via the OAuth client_credentials grant.

    Required env vars:
      - SHOPIFY_SHOP_DOMAIN (e.g. 'baratstunisia.myshopify.com')
      - SHOPIFY_CLIENT_ID
      - SHOPIFY_CLIENT_SECRET

    Returns (token: str, error_msg: str). One of the two is empty.
    Cached for ~50 minutes in module-level dict to avoid re-fetching on every call.
    """
    import urllib.request, urllib.parse, urllib.error, time
    domain = os.environ.get("SHOPIFY_SHOP_DOMAIN", "").strip()
    cid = os.environ.get("SHOPIFY_CLIENT_ID", "").strip()
    csecret = os.environ.get("SHOPIFY_CLIENT_SECRET", "").strip()
    if not (domain and cid and csecret):
        return "", "SHOPIFY_SHOP_DOMAIN / SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET non configurés."

    # Module-level cache to avoid OAuth roundtrip on every cancel
    global _SHOPIFY_TOKEN_CACHE
    try:
        cache = _SHOPIFY_TOKEN_CACHE
    except NameError:
        _SHOPIFY_TOKEN_CACHE = {}
        cache = _SHOPIFY_TOKEN_CACHE
    now = time.time()
    entry = cache.get(domain)
    if entry and entry.get("expires_at", 0) > now + 60:
        return entry["token"], ""

    url = f"https://{domain}/admin/oauth/access_token"
    body = urllib.parse.urlencode({
        "client_id": cid,
        "client_secret": csecret,
        "grant_type": "client_credentials",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            token = data.get("access_token") or ""
            # client_credentials grants are typically valid ~1 hour
            expires_in = int(data.get("expires_in") or 3600)
            cache[domain] = {"token": token, "expires_at": now + expires_in}
            return token, "" if token else "Pas de token dans la réponse Shopify."
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        return "", f"HTTP {e.code}: {raw[:300]}"
    except Exception as e:
        return "", f"Erreur réseau: {e}"


def _shopify_cancel_order(shopify_order_id):
    """Call Shopify Admin API to cancel an order.

    Requires env vars:
      - SHOPIFY_SHOP_DOMAIN (e.g. 'baratstunisia.myshopify.com')
      - SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET (preferred, OAuth flow)
      - OR SHOPIFY_ADMIN_API_TOKEN (legacy custom-app static token)

    Returns (ok: bool, response_data: dict|str).
    """
    import urllib.request, urllib.error
    domain = os.environ.get("SHOPIFY_SHOP_DOMAIN", "").strip()
    if not domain:
        return False, {"_error": "SHOPIFY_SHOP_DOMAIN non configuré."}
    if not shopify_order_id:
        return False, {"_error": "Shopify order ID vide."}

    # Get an access token. Prefer OAuth client_credentials (Dev Dashboard apps).
    token, err = _shopify_get_access_token()
    if not token:
        # Fallback: a directly-set token (custom apps héritées only)
        token = os.environ.get("SHOPIFY_ADMIN_API_TOKEN", "").strip()
    if not token:
        return False, {"_error": err or "Pas de token Shopify disponible."}

    # Strip any 'gid://shopify/Order/' prefix if present, keep only the numeric id
    sid = str(shopify_order_id).strip()
    if "/" in sid:
        sid = sid.rsplit("/", 1)[-1]

    url = f"https://{domain}/admin/api/2024-10/orders/{sid}/cancel.json"
    payload = json.dumps({
        "reason": "customer",
        "email": False,
        "restock": False,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Shopify-Access-Token", token)

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {"_raw": raw}
            return True, data
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"_raw": raw, "_http_status": e.code}
        return False, data
    except Exception as e:
        return False, {"_error": f"Erreur réseau: {e}"}


def _shopify_mark_paid(shopify_order_id, amount, currency="TND"):
    """Call Shopify Admin API to mark an order as paid by creating a
    successful transaction. Useful for Cash on Delivery (COD) orders that
    started as 'pending' — once we collect cash from Navex, we mirror that
    payment status to Shopify so it shows 'Paid' in the merchant dashboard.

    Note: this does NOT actually move money; it's a bookkeeping update.

    Returns (ok: bool, response_data: dict|str).
    """
    import urllib.request, urllib.error
    domain = os.environ.get("SHOPIFY_SHOP_DOMAIN", "").strip()
    if not domain:
        return False, {"_error": "SHOPIFY_SHOP_DOMAIN non configuré."}
    if not shopify_order_id:
        return False, {"_error": "Shopify order ID vide."}

    token, err = _shopify_get_access_token()
    if not token:
        token = os.environ.get("SHOPIFY_ADMIN_API_TOKEN", "").strip()
    if not token:
        return False, {"_error": err or "Pas de token Shopify disponible."}

    sid = str(shopify_order_id).strip()
    if "/" in sid:
        sid = sid.rsplit("/", 1)[-1]

    # POST /admin/api/2024-10/orders/{id}/transactions.json
    # kind="sale" + status="success" mark the order as Paid in Shopify.
    # gateway="manual" indicates the payment was collected outside Shopify
    # (e.g. cash via the delivery service).
    url = f"https://{domain}/admin/api/2024-10/orders/{sid}/transactions.json"
    body = json.dumps({
        "transaction": {
            "kind": "sale",
            "status": "success",
            "amount": str(amount),
            "currency": currency,
            "gateway": "manual",
        }
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Shopify-Access-Token", token)

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {"_raw": raw}
            return True, data
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"_raw": raw, "_http_status": e.code}
        return False, data
    except Exception as e:
        return False, {"_error": f"Erreur réseau: {e}"}


def _extract_shopify_order_id_from_notes(notes):
    """Helper to pull the Shopify order id from the Order.notes string we set
    when receiving the webhook (format: 'shopify_order_id=12345 | ...').
    Returns empty string if not found.
    """
    if not notes:
        return ""
    import re
    m = re.search(r"shopify_order_id=(\d+)", notes)
    return m.group(1) if m else ""


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


# ============================================================================
#                          META ADS SPENDING
# ============================================================================
# Reads ad spend data from Facebook Marketing API and combines it with our
# sales data to compute ROI. Requires env vars:
#   META_ACCESS_TOKEN  — long-lived access token with ads_read permission
#   META_AD_ACCOUNT_ID — ad account id (without 'act_' prefix)
# ============================================================================

def _meta_fetch_spend(start_date, end_date):
    """Fetch ad spend from Meta Marketing API between two dates (inclusive).
    Returns a dict {date_string: spend_amount} like {'2026-05-30': 12.50, ...}
    Returns empty dict on any error.
    """
    import urllib.request, urllib.parse, urllib.error
    token = os.environ.get("META_ACCESS_TOKEN", "").strip()
    account_id = os.environ.get("META_AD_ACCOUNT_ID", "").strip()
    if not token or not account_id:
        return {}
    # Meta wants 'act_<id>' format
    if not account_id.startswith("act_"):
        account_id = f"act_{account_id}"

    # Use the /insights endpoint with daily breakdown
    url = (
        f"https://graph.facebook.com/v18.0/{account_id}/insights"
        f"?fields=spend"
        f"&time_range={{'since':'{start_date}','until':'{end_date}'}}"
        f"&time_increment=1"
        f"&access_token={token}"
    )
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            result = {}
            for entry in data.get("data", []):
                date_start = entry.get("date_start", "")
                spend = entry.get("spend", "0")
                try:
                    result[date_start] = float(spend)
                except (ValueError, TypeError):
                    result[date_start] = 0.0
            return result
    except Exception:
        return {}


@login_required
def ads_dashboard(request):
    """Dashboard showing Meta ad spend vs sales revenue."""
    from datetime import date, timedelta
    today = date.today()
    first_of_month = today.replace(day=1)
    # Default: last 30 days
    start = today - timedelta(days=29)

    # Allow ?month=YYYY-MM filter
    month_filter = request.GET.get("month", "").strip()
    if month_filter:
        try:
            y, m = month_filter.split("-")
            start = date(int(y), int(m), 1)
            # End of month
            if int(m) == 12:
                end_of_month = date(int(y), 12, 31)
            else:
                end_of_month = date(int(y), int(m) + 1, 1) - timedelta(days=1)
            end_date = min(end_of_month, today)
        except Exception:
            end_date = today
    else:
        end_date = today

    start_str = start.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")

    # Fetch from Meta
    spend_by_day = _meta_fetch_spend(start_str, end_str)

    # Compute totals
    total_spend = sum(spend_by_day.values())
    today_spend = spend_by_day.get(today.strftime("%Y-%m-%d"), 0.0)
    month_spend = sum(
        v for d, v in spend_by_day.items()
        if d.startswith(today.strftime("%Y-%m"))
    )

    # Fetch sales (paid ShippingOrders) for the same range
    paid_orders = ShippingOrder.objects.filter(
        status=ShippingOrder.PAID,
        closed_at__date__gte=start,
        closed_at__date__lte=end_date,
    )
    total_revenue = sum(
        float(o.amount_collected or 0) for o in paid_orders
    )

    today_orders = paid_orders.filter(closed_at__date=today)
    today_revenue = sum(float(o.amount_collected or 0) for o in today_orders)

    month_orders = paid_orders.filter(closed_at__date__gte=first_of_month)
    month_revenue = sum(float(o.amount_collected or 0) for o in month_orders)

    # ROAS = revenue / spend (multiplier). ROI = (revenue - spend) / spend (%)
    def safe_div(a, b):
        return (a / b) if b else 0
    roas_total = safe_div(total_revenue, total_spend)
    roi_total = safe_div(total_revenue - total_spend, total_spend) * 100
    roas_today = safe_div(today_revenue, today_spend)
    roas_month = safe_div(month_revenue, month_spend)

    # Build daily rows for the table — combine spend + revenue per day
    revenue_by_day = {}
    for o in paid_orders:
        if not o.closed_at:
            continue
        d = o.closed_at.strftime("%Y-%m-%d")
        revenue_by_day[d] = revenue_by_day.get(d, 0) + float(o.amount_collected or 0)

    all_dates = sorted(set(list(spend_by_day.keys()) + list(revenue_by_day.keys())), reverse=True)
    rows = []
    for d in all_dates:
        s = spend_by_day.get(d, 0)
        r = revenue_by_day.get(d, 0)
        rows.append({
            "date": d,
            "spend": s,
            "revenue": r,
            "roas": safe_div(r, s),
            "profit": r - s,
        })

    has_token = bool(os.environ.get("META_ACCESS_TOKEN", "").strip())
    has_account = bool(os.environ.get("META_AD_ACCOUNT_ID", "").strip())

    return render(request, "inventory/ads_dashboard.html", {
        "rows": rows,
        "total_spend": total_spend,
        "total_revenue": total_revenue,
        "today_spend": today_spend,
        "today_revenue": today_revenue,
        "month_spend": month_spend,
        "month_revenue": month_revenue,
        "roas_total": roas_total,
        "roi_total": roi_total,
        "roas_today": roas_today,
        "roas_month": roas_month,
        "start": start_str,
        "end": end_str,
        "has_token": has_token,
        "has_account": has_account,
    })
