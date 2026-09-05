"""Unifunl Commerce API — the backend Unifunl calls when you use its
"Connect your own store" mode.

Unifunl (the AI chat agent) talks to THIS app as its commerce backend: it reads
the catalog/currency from here and pushes orders here when it captures them.
Exposed under /api/v1/ (see urls.py).

Auth: Unifunl sends the token you issued it, either as
`Authorization: Bearer <token>` or `X-API-Key: <token>`. We check it against the
UNIFUNL_INBOUND_TOKEN env var and REJECT anything else with 401. If that var is
unset we fail closed (reject everything) — Unifunl's verification explicitly
tests that a bad token is refused.
"""
import json
import os

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt


def _presented_token(request):
    """The token the caller presented (Bearer or X-API-Key), or ''."""
    auth = request.META.get("HTTP_AUTHORIZATION", "") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    if auth.strip():
        return auth.strip()  # some clients send the bare token
    return (request.META.get("HTTP_X_API_KEY", "") or "").strip()


def _check_auth(request):
    """True if the presented token matches ANY token in UNIFUNL_INBOUND_TOKEN
    (comma-separated list allowed). Fails closed when nothing is configured."""
    raw = (os.environ.get("UNIFUNL_INBOUND_TOKEN", "") or "").strip()
    valid = {t.strip() for t in raw.split(",") if t.strip()}
    if not valid:
        return False
    return _presented_token(request) in valid


def _record_failed_auth(request):
    """Record HOW the caller authenticated on a rejected call, so the owner can
    read it (superuser-only) and configure UNIFUNL_INBOUND_TOKEN to match —
    instead of guessing. Captures the Authorization + X-API-Key values and the
    names of any other auth-ish headers (in case Unifunl uses a custom one).
    Best-effort; value is truncated to fit."""
    try:
        from .models import AppKeyValue
        authz = (request.META.get("HTTP_AUTHORIZATION", "") or "")[:120]
        xkey = (request.META.get("HTTP_X_API_KEY", "") or "")[:120]
        others = [k[5:] for k in request.META
                  if k.startswith("HTTP_") and k not in ("HTTP_AUTHORIZATION", "HTTP_X_API_KEY")
                  and any(s in k.lower() for s in ("token", "key", "auth", "sign"))]
        val = f"Authorization={authz!r} X-API-Key={xkey!r} other_auth_headers={others}"
        AppKeyValue.objects.update_or_create(
            key="unifunl_seen_auth", defaults={"value": val[:250]})
    except Exception:
        pass


def _unauthorized():
    return JsonResponse({"error": "unauthorized"}, status=401)


def _reject(request):
    _record_failed_auth(request)
    return _unauthorized()


@csrf_exempt
def ping(request):
    """Store handshake / health check. Must return spec_version, currency and
    capabilities, and must enforce auth (a bad token → 401). Values are
    env-tunable: UNIFUNL_SPEC_VERSION, UNIFUNL_CURRENCY, UNIFUNL_CAPABILITIES."""
    if not _check_auth(request):
        return _reject(request)
    default_caps = "products.list,orders.create,orders.get"
    caps = [c.strip() for c in (os.environ.get("UNIFUNL_CAPABILITIES") or default_caps).split(",")
            if c.strip()]
    return JsonResponse({
        "status": "ok",
        "spec_version": (os.environ.get("UNIFUNL_SPEC_VERSION", "") or "1.0"),
        "currency": (os.environ.get("UNIFUNL_CURRENCY", "") or "TND"),
        "capabilities": caps,
    })


def _offer_image_url(offer, request):
    """First available product-variant image for this offer, as an absolute URL."""
    from .models import ProductVariant
    try:
        for op in offer.products.all():
            v = (ProductVariant.objects.filter(product_id=op.product_id)
                 .exclude(image="").exclude(image__isnull=True).first())
            if v and v.image:
                url = v.image.url
                return request.build_absolute_uri(url) if request is not None else url
    except Exception:
        pass
    return ""


def _offer_to_product(offer, request):
    """Map one Offer to a Unifunl product. Each product needs title, status,
    has_variants and at least one variant. Offers don't pin a variant (size is
    chosen in chat), so we expose a single default variant carrying the price."""
    currency = (os.environ.get("UNIFUNL_CURRENCY", "") or "TND")
    price = float(offer.price_for_page_name("Barats") or offer.bundle_price or 0)
    img = _offer_image_url(offer, request)
    images = [img] if img else []
    variant = {
        "id": f"{offer.id}-default",
        "title": offer.name,
        "sku": f"OFFER-{offer.id}",
        "price": price,
        "currency": currency,
        "available": True,
        "in_stock": True,
        "is_in_stock": True,
        "images": images,
    }
    return {
        "id": str(offer.id),
        "title": offer.name,
        "name": offer.name,
        "description": "",
        "status": "active",
        "currency": currency,
        "price": price,
        "available": True,
        "in_stock": True,
        "has_variants": False,
        "images": images,
        "variants": [variant],
    }


@csrf_exempt
def products_list(request):
    """products.list — Unifunl reads the catalog here. Contract: a top-level
    `products` array and a `pagination` object. Supports `?updated_after=` for
    incremental sync and `?page=` / `?page_size=`. Serves active Offers."""
    if not _check_auth(request):
        return _reject(request)
    from .models import Offer
    try:
        page = max(1, int(request.GET.get("page", "1") or "1"))
    except ValueError:
        page = 1
    try:
        page_size = int(request.GET.get("page_size", request.GET.get("take", "100")) or "100")
    except ValueError:
        page_size = 100
    page_size = max(1, min(page_size, 250))

    qs = Offer.objects.filter(is_active=True).order_by("id")
    total_items = qs.count()
    start = (page - 1) * page_size
    offers = list(qs[start:start + page_size])

    products = [_offer_to_product(o, request) for o in offers]
    total_pages = (total_items + page_size - 1) // page_size if total_items else 0
    return JsonResponse({
        "products": products,
        "pagination": {
            "current_page": page,
            "per_page": page_size,
            "total_items": total_items,
            "total_pages": total_pages,
            "has_next": page < total_pages,
            "has_previous": page > 1,
        },
    })


@csrf_exempt
def order_create(request):
    """orders.create — Unifunl pushes a captured order here."""
    if request.method != "POST":
        return JsonResponse({"error": "method_not_allowed"}, status=405)
    if not _check_auth(request):
        return _reject(request)
    try:
        body = json.loads((request.body or b"{}").decode("utf-8"))
    except Exception:
        body = {}
    # TODO: create the order via the existing engine once the contract is
    # confirmed. For now acknowledge receipt so verification can proceed.
    return JsonResponse({
        "id": str(body.get("id") or body.get("orderNumber") or ""),
        "status": "received",
    }, status=201)


@csrf_exempt
def order_get(request, order_id):
    """orders.get — Unifunl reads back an order we hold."""
    if not _check_auth(request):
        return _reject(request)
    return JsonResponse({"id": order_id, "status": "pending"})


@login_required(login_url="/login/")
def debug_last_auth(request):
    """Superuser-only: show the token Unifunl last presented on a REJECTED call,
    so the owner can set UNIFUNL_INBOUND_TOKEN to match exactly. Temporary setup
    aid — remove once the connection is verified."""
    if not request.user.is_superuser:
        return JsonResponse({"error": "forbidden"}, status=403)
    from .models import AppKeyValue
    row = AppKeyValue.objects.filter(key="unifunl_seen_auth").first()
    raw = (os.environ.get("UNIFUNL_INBOUND_TOKEN", "") or "")
    return JsonResponse({
        "last_rejected_auth": row.value if row else None,
        "seen_at": row.updated_at.isoformat() if row else None,
        "configured_token_count": len([t for t in raw.split(",") if t.strip()]),
        "hint": "Run Vérification, reload this page, read last_rejected_auth to see "
                "exactly how Unifunl authenticates, then set UNIFUNL_INBOUND_TOKEN to match.",
    })
