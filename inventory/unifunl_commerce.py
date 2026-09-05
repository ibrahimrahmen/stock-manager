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
