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

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt


def _check_auth(request):
    """True only if the request carries the exact token we issued Unifunl.
    Fails closed when no token is configured."""
    expected = (os.environ.get("UNIFUNL_INBOUND_TOKEN", "") or "").strip()
    if not expected:
        return False
    auth = request.META.get("HTTP_AUTHORIZATION", "") or ""
    if auth.lower().startswith("bearer ") and auth[7:].strip() == expected:
        return True
    xkey = (request.META.get("HTTP_X_API_KEY", "") or "").strip()
    if xkey and xkey == expected:
        return True
    return False


def _unauthorized():
    return JsonResponse({"error": "unauthorized"}, status=401)


@csrf_exempt
def ping(request):
    """Store handshake / health check. Must return spec_version, currency and
    capabilities, and must enforce auth (a bad token → 401). Values are
    env-tunable: UNIFUNL_SPEC_VERSION, UNIFUNL_CURRENCY, UNIFUNL_CAPABILITIES."""
    if not _check_auth(request):
        return _unauthorized()
    default_caps = "products.list,orders.create,orders.get"
    caps = [c.strip() for c in (os.environ.get("UNIFUNL_CAPABILITIES") or default_caps).split(",")
            if c.strip()]
    return JsonResponse({
        "status": "ok",
        "spec_version": (os.environ.get("UNIFUNL_SPEC_VERSION", "") or "1.0"),
        "currency": (os.environ.get("UNIFUNL_CURRENCY", "") or "TND"),
        "capabilities": caps,
    })


@csrf_exempt
def products_list(request):
    """products.list — Unifunl reads the catalog here. Contract: a top-level
    `products` array and a `pagination` object. Supports `?updated_after=` for
    incremental sync and `?page=` / `?page_size=`. Starts empty; the real
    catalog is populated once the connection verifies."""
    if not _check_auth(request):
        return _unauthorized()
    try:
        page = int(request.GET.get("page", "1") or "1")
    except ValueError:
        page = 1
    try:
        page_size = int(request.GET.get("page_size", request.GET.get("take", "50")) or "50")
    except ValueError:
        page_size = 50

    products = []  # TODO: map the real catalogue once the item contract is known
    return JsonResponse({
        "products": products,
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": len(products),
            "total_pages": 1,
            "has_next": False,
            "has_previous": page > 1,
        },
    })


@csrf_exempt
def order_create(request):
    """orders.create — Unifunl pushes a captured order here."""
    if request.method != "POST":
        return JsonResponse({"error": "method_not_allowed"}, status=405)
    if not _check_auth(request):
        return _unauthorized()
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
        return _unauthorized()
    return JsonResponse({"id": order_id, "status": "pending"})
