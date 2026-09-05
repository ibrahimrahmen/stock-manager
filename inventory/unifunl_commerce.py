"""Unifunl Commerce API v1 — the backend Unifunl calls in "Connect your own
store" mode. Implements the endpoints from the Unifunl Commerce API v1 contract:
GET /ping, GET /products, GET /products/{identifier}, POST /orders,
GET /orders/{order_id}. Exposed under /api/v1/ (see urls.py).

Key contract rules honoured here:
- Money is decimal STRINGS with the currency's decimal places (TND -> 3).
- Errors are {"error": {"code","message","details?"}} with stable codes.
- Every response repeats `currency`.
- Prices are server-authoritative: prices sent by the client are ignored.
- Order creation is atomic and idempotent on external_order_id.

Auth: Bearer or X-API-Key, checked against UNIFUNL_INBOUND_TOKEN (comma-
separated list allowed). Fails closed with 401 when unset/wrong.
"""
import json
import os
from decimal import Decimal, ROUND_HALF_UP

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

DECIMALS = 3  # TND has 3 decimal places


def _currency():
    return os.environ.get("UNIFUNL_CURRENCY", "") or "TND"


def _money(v):
    """Format a value as a decimal string with the currency's decimal places."""
    try:
        d = Decimal(str(v))
    except Exception:
        d = Decimal("0")
    q = Decimal(1).scaleb(-DECIMALS)  # 0.001 for 3 dp
    return str(d.quantize(q, rounding=ROUND_HALF_UP))


def _now_iso():
    return timezone.now().isoformat()


def _err(code, message, status, details=None):
    e = {"code": code, "message": message}
    if details is not None:
        e["details"] = details
    return JsonResponse({"error": e}, status=status)


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def _presented_token(request):
    auth = request.META.get("HTTP_AUTHORIZATION", "") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    if auth.strip():
        return auth.strip()
    return (request.META.get("HTTP_X_API_KEY", "") or "").strip()


def _check_auth(request):
    raw = (os.environ.get("UNIFUNL_INBOUND_TOKEN", "") or "").strip()
    valid = {t.strip() for t in raw.split(",") if t.strip()}
    if not valid:
        return False
    return _presented_token(request) in valid


def _reject(request):
    # capture how the caller authenticated, for setup diagnostics (superuser)
    try:
        from .models import AppKeyValue
        authz = (request.META.get("HTTP_AUTHORIZATION", "") or "")[:120]
        xkey = (request.META.get("HTTP_X_API_KEY", "") or "")[:120]
        AppKeyValue.objects.update_or_create(
            key="unifunl_seen_auth",
            defaults={"value": f"Authorization={authz!r} X-API-Key={xkey!r}"[:250]})
    except Exception:
        pass
    return _err("unauthorized", "Token manquant ou invalide.", 401)


# --------------------------------------------------------------------------- #
# Catalogue mapping (Offers -> Unifunl products)
# --------------------------------------------------------------------------- #
def _offer_description(offer):
    """The offer's own description if set; otherwise built from its products'
    AI descriptions, deduped per SKU family (parent + versions)."""
    own = (offer.description or "").strip()
    if own:
        return own
    by_root = {}
    for op in offer.products.all():
        p = op.product
        if not p:
            continue
        root = p.parent_product_id or p.id
        d = (p.description or "").strip()
        if d and root not in by_root:
            by_root[root] = d
    return "\n\n".join(by_root.values())


def _abs(u, request):
    return request.build_absolute_uri(u) if request is not None else u


def _offer_image_urls(offer, request, limit=10):
    """Images to send for the offer. If the offer has its OWN colour photos (the
    ensemble worn together, per colour) we send ONLY those — that's what a
    customer asking about the offer must see, not the individual products'
    photos. Otherwise fall back to the offer cover + the products' colour photos.
    """
    urls, seen = [], set()
    own = list(offer.images.all())
    if own:
        for oi in own:
            if not oi.image:
                continue
            try:
                u = oi.image.url
            except Exception:
                continue
            if u and u not in seen:
                seen.add(u)
                urls.append(_abs(u, request))
                if len(urls) >= limit:
                    break
        return urls
    # Legacy fallback: offer cover first, then the products' colour photos.
    try:
        if offer.image:
            try:
                u = offer.image.url
                seen.add(u)
                urls.append(_abs(u, request))
            except Exception:
                pass
        for op in offer.products.all():
            p = op.product
            if not p:
                continue
            for v in p.variants.all():
                if not v.image:
                    continue
                try:
                    u = v.image.url
                except Exception:
                    continue
                if not u or u in seen:
                    continue
                seen.add(u)
                urls.append(_abs(u, request))
                if len(urls) >= limit:
                    return urls
    except Exception:
        pass
    return urls


def _offer_updated_at(offer):
    return (getattr(offer, "updated_at", None) or offer.created_at or timezone.now()).isoformat()


def _offer_variants(offer, request, price):
    """One Unifunl variant per colour of the OFFER. If the offer has its own
    colour photos (the ensemble per colour), use those — each variant carries the
    ensemble photo for that colour. Otherwise fall back to the distinct colours of
    the offer's products. Price is the offer price for every colour; size is
    chosen in chat, not a variant axis."""
    own = list(offer.images.all())
    if own:
        variants = []
        for oi in own:
            key = (oi.color_name or oi.color_label or "").strip().upper() or f"C{oi.id}"
            img = None
            if oi.image:
                try:
                    img = _abs(oi.image.url, request)
                except Exception:
                    img = None
            variants.append({
                "id": f"{offer.id}-{key}",
                "sku": f"OFFER-{offer.id}-{key}",
                "price": _money(price),
                "compare_at_price": None,
                "inventory_quantity": None,
                "is_in_stock": True,
                "options": {"Couleur": oi.color_label or oi.color_name or key},
                "image": img,
            })
        return variants

    seen = {}
    for op in offer.products.all():
        p = op.product
        if not p:
            continue
        for v in p.variants.all():
            key = (v.color_name or "").strip().upper()
            if not key or key in seen:
                continue
            img = None
            if v.image:
                try:
                    img = request.build_absolute_uri(v.image.url) if request is not None else v.image.url
                except Exception:
                    img = None
            seen[key] = {
                "id": f"{offer.id}-{key}",
                "sku": f"OFFER-{offer.id}-{key}",
                "price": _money(price),
                "compare_at_price": None,
                "inventory_quantity": None,  # not tracked at offer level -> sellable
                "is_in_stock": True,
                "options": {"Couleur": v.color_label or key},
                "image": img,
            }
    variants = list(seen.values())
    if not variants:
        imgs = _offer_image_urls(offer, request, limit=1)
        variants = [{
            "id": "default",
            "sku": f"OFFER-{offer.id}",
            "price": _money(price),
            "compare_at_price": None,
            "inventory_quantity": None,
            "is_in_stock": True,
            "options": {},
            "image": imgs[0] if imgs else None,
        }]
    return variants


def _offer_to_product(offer, request):
    """Map one Offer to a Unifunl product object (v1 contract)."""
    price = offer.price_for_page_name("Barats") or offer.bundle_price or 0
    images = _offer_image_urls(offer, request)
    variants = _offer_variants(offer, request, price)
    return {
        "id": str(offer.id),
        "sku": f"OFFER-{offer.id}",
        "title": offer.name,
        "description": _offer_description(offer),
        "status": "active",
        "images": images,
        "has_variants": len(variants) > 1,
        "variants": variants,
        "updated_at": _offer_updated_at(offer),
    }


def _offer_page_names():
    """Sales-page names whose offers are sent to Unifunl. Default: Barats only.
    Override with UNIFUNL_OFFER_PAGES (comma-separated). Empty => all offers."""
    raw = os.environ.get("UNIFUNL_OFFER_PAGES", "Barats,Barats.tn")
    return [s.strip() for s in (raw or "").split(",") if s.strip()]


def _barats_offers_qs():
    """Active offers linked to the configured Barats page(s)."""
    from django.db.models import Q
    from .models import Offer
    qs = Offer.objects.filter(is_active=True)
    pages = _offer_page_names()
    if pages:
        q = Q()
        for nm in pages:
            q |= Q(sales_pages__name__iexact=nm)
        qs = qs.filter(q).distinct()
    return qs


def _is_offer_sent(offer):
    """True if this offer belongs to the Barats page(s) we send to Unifunl."""
    pages = {p.lower() for p in _offer_page_names()}
    if not pages:
        return offer.is_active
    names = {(sp.name or "").lower() for sp in offer.sales_pages.all()}
    return offer.is_active and bool(pages & names)


def _clamp_int(val, default, lo, hi):
    try:
        n = int(val)
    except (TypeError, ValueError):
        return default
    return max(lo, min(n, hi))


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@csrf_exempt
def ping(request):
    """Health check + capability discovery + store settings."""
    if not _check_auth(request):
        return _reject(request)
    return JsonResponse({
        "service": os.environ.get("UNIFUNL_SERVICE", "") or "Barats",
        "spec_version": "1.0",
        "currency": _currency(),
        "shipping_amount": _money(os.environ.get("UNIFUNL_SHIPPING", "7")),
        "capabilities": [
            "products.list", "products.get", "orders.create", "orders.get",
        ],
        "rate_limit": {"requests": 120, "window_seconds": 60},
        "time": _now_iso(),
    })


@csrf_exempt
def products_list(request):
    """Paginated catalogue of active Offers."""
    if not _check_auth(request):
        return _reject(request)
    from .models import Offer

    page = _clamp_int(request.GET.get("page", "1"), 1, 1, 10_000_000)
    limit = _clamp_int(request.GET.get("limit", request.GET.get("page_size", "50")),
                       50, 1, 200)  # clamp, never error
    search = (request.GET.get("search") or "").strip()
    updated_after = (request.GET.get("updated_after") or "").strip()

    qs = (_barats_offers_qs()
          .prefetch_related("images", "products__product__variants").order_by("id"))
    if search:
        qs = qs.filter(name__icontains=search)
    if updated_after:
        try:
            from django.utils.dateparse import parse_datetime
            dt = parse_datetime(updated_after)
            if dt:
                qs = qs.filter(created_at__gte=dt)  # best-effort incremental sync
        except Exception:
            pass

    total_items = qs.count()
    start = (page - 1) * limit
    offers = list(qs[start:start + limit])
    products = [_offer_to_product(o, request) for o in offers]
    total_pages = (total_items + limit - 1) // limit if total_items else 0
    return JsonResponse({
        "products": products,
        "pagination": {
            "current_page": page,
            "per_page": limit,
            "total_items": total_items,
            "total_pages": total_pages,
        },
        "currency": _currency(),
    })


@csrf_exempt
def product_get(request, identifier):
    """Resolve a single product by id, then SKU (OFFER-<id>), then name."""
    if not _check_auth(request):
        return _reject(request)
    from .models import Offer
    offer = None
    ident = (identifier or "").strip()
    if ident.isdigit():
        offer = Offer.objects.filter(pk=int(ident)).first()
    if offer is None and ident.upper().startswith("OFFER-"):
        tail = ident.split("-", 1)[1]
        if tail.isdigit():
            offer = Offer.objects.filter(pk=int(tail)).first()
    if offer is None:
        offer = Offer.objects.filter(name__iexact=ident).first()
    if offer is None or not _is_offer_sent(offer):
        return _err("product_not_found", "Produit introuvable.", 404)
    return JsonResponse({"product": _offer_to_product(offer, request),
                         "currency": _currency()})


def _resolve_offer(pid):
    """Resolve a line_item product_id (id or SKU) to an active Offer."""
    from .models import Offer
    pid = str(pid or "").strip()
    if pid.isdigit():
        return Offer.objects.filter(pk=int(pid)).first()
    if pid.upper().startswith("OFFER-"):
        tail = pid.split("-", 1)[1]
        if tail.isdigit():
            return Offer.objects.filter(pk=int(tail)).first()
    return Offer.objects.filter(name__iexact=pid).first()


def _unifunl_status(order):
    from .models import Order
    m = {
        Order.NON_CONFIRMEE: ("pending", "Commande reçue, en attente de confirmation"),
        Order.CONFIRMEE: ("confirmed", "Confirmée, en préparation"),
        Order.EN_COURS: ("shipped", "Expédiée"),
        Order.AU_MAGASIN: ("shipped", "En cours de livraison"),
        Order.LIVREE: ("delivered", "Livrée"),
        Order.PAYEE: ("delivered", "Livrée et payée"),
        Order.RETURNING: ("returned", "En retour"),
        Order.RETURNED: ("returned", "Retournée"),
        Order.ANNULEE: ("cancelled", "Annulée"),
    }
    return m.get(order.status, ("pending", "Commande reçue"))


def _external_id_from_notes(order):
    for part in (order.notes or "").split("|"):
        part = part.strip()
        if part.startswith("shopify_order_id=unifunl:"):
            return part.split("unifunl:", 1)[1].strip()
    return ""


def _order_response(order, http_status):
    """Build the v1 order response from one of our Orders."""
    status, label = _unifunl_status(order)
    subtotal = Decimal("0")
    line_items = []
    for oo in order.order_offers.all():
        qty = oo.quantity or 1
        unit = Decimal(str(oo.bundle_price or 0))
        line_total = unit * qty
        subtotal += line_total
        line_items.append({
            "product_id": str(oo.offer_id or ""),
            "variant_id": "default",
            "sku": f"OFFER-{oo.offer_id}" if oo.offer_id else "",
            "product_name": oo.offer_name or "",
            "quantity": qty,
            "unit_price": _money(unit),
            "line_total": _money(line_total),
        })
    shipping = Decimal(str(order.delivery_fee or 0))
    discount = Decimal(str(order.discount or 0))
    tax = Decimal("0")
    total = subtotal - discount + shipping + tax
    cust = order.customer
    data = {
        "order_id": str(order.id),
        "external_order_id": _external_id_from_notes(order),
        "status": status,
        "status_label": label,
        "created_at": order.created_at.isoformat() if order.created_at else _now_iso(),
        "updated_at": order.updated_at.isoformat() if getattr(order, "updated_at", None) else _now_iso(),
        "currency": _currency(),
        "customer": {
            "first_name": (order.customer_name or (cust.name if cust else "") or "").split(" ")[0],
            "last_name": "",
            "phone": (cust.phone if cust else ""),
        },
        "shipping_address": {
            "address_line": order.address or "",
            "city": order.ville or "",
            "state_region": (order.region.name if order.region_id else ""),
        },
        "payment_method": "cod",
        "subtotal": _money(subtotal),
        "discount": _money(discount),
        "shipping_cost": _money(shipping),
        "tax": _money(tax),
        "total": _money(total),
        "line_items": line_items,
    }
    return JsonResponse({"order": data}, status=http_status)


@csrf_exempt
def order_create(request):
    """Create a real order (server-authoritative pricing, atomic, idempotent)."""
    if request.method != "POST":
        return _err("invalid_request", "POST requis.", 400)
    if not _check_auth(request):
        return _reject(request)
    from .models import Order, SalesPage

    try:
        body = json.loads((request.body or b"{}").decode("utf-8"))
    except Exception:
        return _err("invalid_request", "JSON invalide.", 400)

    ext = str(body.get("external_order_id") or "").strip()
    if not ext:
        return _err("validation_failed", "external_order_id requis.", 422,
                    {"fields": ["external_order_id"]})

    # Idempotency: same external_order_id -> return the existing order.
    note_key = f"shopify_order_id=unifunl:{ext}"
    existing = Order.objects.filter(notes__contains=note_key).first()
    if existing:
        return _order_response(existing, 200)

    customer = body.get("customer") or {}
    phone = str(customer.get("phone") or "").strip()
    if not phone:
        return _err("validation_failed", "Téléphone client requis.", 422,
                    {"fields": ["customer.phone"]})
    addr = body.get("shipping_address") or {}
    line_items = body.get("line_items") or []
    if not line_items:
        return _err("validation_failed", "line_items requis.", 422,
                    {"fields": ["line_items"]})

    # Resolve + validate every line BEFORE writing anything (atomic).
    resolved = []
    for i, li in enumerate(line_items):
        offer = _resolve_offer(li.get("product_id"))
        if offer is None:
            return _err("product_not_found", "Produit introuvable.", 422,
                        {"line_item_index": i})
        if not offer.is_active:
            return _err("product_not_available", "Produit indisponible.", 422,
                        {"line_item_index": i})
        try:
            qty = max(1, int(li.get("quantity") or 1))
        except (TypeError, ValueError):
            qty = 1
        price = offer.price_for_page_name("Barats") or offer.bundle_price or Decimal("0")
        # variant_id we sent is "<offer_id>-<COLOUR>"; recover the colour so the
        # order records which colour the customer chose.
        vid = str(li.get("variant_id") or "")
        colour = ""
        if "-" in vid:
            head, tail = vid.split("-", 1)
            if head.isdigit() and tail.lower() != "default":
                colour = tail
        resolved.append((offer, qty, Decimal(str(price)), colour))

    first = (customer.get("first_name") or "").strip()
    last = (customer.get("last_name") or "").strip()
    name = (first + " " + last).strip()
    shipping_lines = [{"price": _money(os.environ.get("UNIFUNL_SHIPPING", "7"))}]
    payload = {
        "order_number": ext, "name": ext, "note": (body.get("note") or ""),
        "phone": phone,
        "shipping_address": {
            "first_name": first, "last_name": last, "phone": phone,
            "address1": addr.get("address_line") or "",
            "city": addr.get("city") or "",
            "province": addr.get("state_region") or addr.get("city") or "",
            "country": addr.get("country") or "TN",
        },
        "customer": {"phone": phone, "first_name": first, "last_name": last},
        "line_items": [{
            "title": (o.name + (f" - {col}" if col else "")),
            "name": o.name, "variant_title": col,
            "quantity": q, "price": _money(pr),
        } for (o, q, pr, col) in resolved],
        "shipping_lines": shipping_lines,
    }
    payload["billing_address"] = payload["shipping_address"]

    sp = (SalesPage.objects.filter(pk=3).first()
          or SalesPage.objects.filter(name__iexact="Barats").first()
          or SalesPage.objects.filter(name__iexact="Barats.tn").first())
    sp_id = sp.id if sp else None

    from .views import _create_order_from_shopify_shaped_payload
    try:
        with transaction.atomic():
            _create_order_from_shopify_shaped_payload(
                payload, source="messenger", external_id=f"unifunl:{ext}",
                sales_page_id=sp_id)
    except Exception as e:
        return _err("internal_error", str(e)[:200], 500)

    order = (Order.objects.filter(notes__contains=note_key)
             .prefetch_related("order_offers").select_related("customer", "region")
             .order_by("-id").first())
    if not order:
        return _err("internal_error", "Commande créée mais introuvable.", 500)
    return _order_response(order, 201)


@csrf_exempt
def order_get(request, order_id):
    if not _check_auth(request):
        return _reject(request)
    from .models import Order
    order = (Order.objects.filter(pk=order_id)
             .prefetch_related("order_offers").select_related("customer", "region").first()
             if str(order_id).isdigit() else None)
    if order is None:
        return _err("order_not_found", "Commande introuvable.", 404)
    return _order_response(order, 200)


@login_required(login_url="/login/")
def debug_last_auth(request):
    """Superuser-only setup aid: shows how Unifunl last authenticated (rejected)."""
    if not request.user.is_superuser:
        return JsonResponse({"error": "forbidden"}, status=403)
    from .models import AppKeyValue
    row = AppKeyValue.objects.filter(key="unifunl_seen_auth").first()
    order_row = AppKeyValue.objects.filter(key="unifunl_last_order_payload").first()
    raw = (os.environ.get("UNIFUNL_INBOUND_TOKEN", "") or "")
    return JsonResponse({
        "last_rejected_auth": row.value if row else None,
        "seen_at": row.updated_at.isoformat() if row else None,
        "configured_token_count": len([t for t in raw.split(",") if t.strip()]),
        "last_order_payload": order_row.value if order_row else None,
    })
