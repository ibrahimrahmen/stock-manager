"""Unifunl Commerce API — the backend Unifunl calls when you use its
"Connect your own store" mode.

Unifunl (the AI chat agent) talks to THIS app as its commerce backend: it reads
the catalog/currency from here and pushes orders here when it captures them.
Exposed under /api/v1/ (see urls.py). Endpoints are discovered from Unifunl's
verification step and added incrementally.

Auth: Unifunl sends the token you issued it, either as
`Authorization: Bearer <token>` or `X-API-Key: <token>`. We check it against the
UNIFUNL_INBOUND_TOKEN env var. (When that var is unset we allow requests, so the
very first connectivity checks work before the token is configured.)
"""
import json
import os

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt


def _check_auth(request):
    """True if the request carries the token we issued Unifunl (or if no token
    is configured yet)."""
    expected = (os.environ.get("UNIFUNL_INBOUND_TOKEN", "") or "").strip()
    if not expected:
        return True  # not configured yet — don't block initial setup
    auth = request.META.get("HTTP_AUTHORIZATION", "") or ""
    if auth.lower().startswith("bearer "):
        if auth[7:].strip() == expected:
            return True
    xkey = (request.META.get("HTTP_X_API_KEY", "") or "").strip()
    if xkey and xkey == expected:
        return True
    return False


@csrf_exempt
def ping(request):
    """Store handshake hit by Unifunl's verification step. Must return the
    store's spec_version, currency and capabilities (Unifunl reads currency +
    features from here). Values are env-tunable so we can adjust without a code
    change as we learn Unifunl's expected vocabulary:
      UNIFUNL_SPEC_VERSION (default '1.0')
      UNIFUNL_CURRENCY      (default 'TND')
      UNIFUNL_CAPABILITIES  (comma-separated; default empty)
    """
    # Unifunl requires at least these three; they map to the endpoints below.
    default_caps = "products.list,orders.create,orders.get"
    caps = [c.strip() for c in (os.environ.get("UNIFUNL_CAPABILITIES") or default_caps).split(",")
            if c.strip()]
    return JsonResponse({
        "status": "ok",
        "spec_version": (os.environ.get("UNIFUNL_SPEC_VERSION", "") or "1.0"),
        "currency": (os.environ.get("UNIFUNL_CURRENCY", "") or "TND"),
        "capabilities": caps,
    })


def _unauthorized():
    return JsonResponse({"error": "unauthorized"}, status=401)


@csrf_exempt
def products_list(request):
    """products.list — Unifunl reads the catalog here. Returns a paginated list
    in the same envelope Unifunl uses elsewhere ({data, meta}). Starts empty;
    real catalog is populated once the connection verifies and we confirm the
    exact product/variant contract Unifunl expects."""
    if not _check_auth(request):
        return _unauthorized()
    try:
        page = int(request.GET.get("page", "1") or "1")
    except ValueError:
        page = 1
    try:
        take = int(request.GET.get("take", "50") or "50")
    except ValueError:
        take = 50
    data = []  # TODO: map real products once the contract is confirmed
    return JsonResponse({
        "data": data,
        "meta": {
            "page": page, "take": take, "itemCount": len(data),
            "pageCount": 1, "hasPreviousPage": page > 1, "hasNextPage": False,
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
