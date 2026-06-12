"""Converty OAuth 2.0 integration.

Two-way sync with the seller's Converty store:
  - Inbound:  order.create / order.update webhooks  -> create/update v2 Orders
  - Outbound: confirm / cancel / livrée in our system -> PATCH Converty status

client_id / client_secret are read from environment variables. Per-store
tokens live in the ConvertyConnection model. Access tokens last 15 days and
are refreshed proactively with a 5-minute buffer.
"""
import os
import json
import secrets
import urllib.parse
import urllib.request
import urllib.error
from datetime import timedelta

from django.conf import settings
from django.http import JsonResponse, HttpResponseRedirect, HttpResponseBadRequest
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required

BASE = "https://partner.converty.shop"
AUTHORIZE_URL = f"{BASE}/oauth2/authorize"
TOKEN_URL = f"{BASE}/oauth2/token"
API = f"{BASE}/api/v1"

SCOPES = "read-orders create-orders update-orders read-products read-stores read-hooks create-hooks delete-hooks"

# Our status -> Converty status (only these three are pushed back)
STATUS_MAP = {
    "confirmee": "confirmed",
    "annulee":   "rejected",
    "livree":    "delivered",
}


def _client_id():
    return os.environ.get("CONVERTY_CLIENT_ID", "")


def _client_secret():
    return os.environ.get("CONVERTY_CLIENT_SECRET", "")


def _redirect_uri(request):
    # Build from the current host so it matches what was registered with Converty.
    return request.build_absolute_uri("/converty/callback/")


# ---------------------------------------------------------------------------
# Low-level HTTP helpers (stdlib only, no extra deps)
# ---------------------------------------------------------------------------
def _post_form(url, data):
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {"success": False, "message": str(e)}
    except Exception as e:
        return 0, {"success": False, "message": str(e)}


def _api_request(method, path, token, json_body=None):
    url = f"{API}{path}"
    data = json.dumps(json_body).encode("utf-8") if json_body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {"success": False, "message": str(e)}
    except Exception as e:
        return 0, {"success": False, "message": str(e)}


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------
def _store_tokens(conn, token_data):
    conn.access_token = token_data.get("access_token", "")
    conn.refresh_token = token_data.get("refresh_token", conn.refresh_token)
    expires_in = int(token_data.get("expires_in", 0) or 0)
    conn.access_token_expires_at = timezone.now() + timedelta(seconds=expires_in)
    conn.is_active = True
    conn.save()


def get_valid_converty_token():
    """Return a valid access token, refreshing if it expires within 5 minutes.
    Returns None if no active connection or refresh fails."""
    from .models import ConvertyConnection
    conn = ConvertyConnection.objects.filter(is_active=True).order_by("-updated_at").first()
    if not conn or not conn.access_token:
        return None
    buffer = timedelta(minutes=5)
    if conn.access_token_expires_at and conn.access_token_expires_at - buffer > timezone.now():
        return conn.access_token
    # Refresh
    status, data = _post_form(TOKEN_URL, {
        "grant_type": "refresh_token",
        "refresh_token": conn.refresh_token,
        "client_id": _client_id(),
        "client_secret": _client_secret(),
    })
    if status == 200 and data.get("access_token"):
        _store_tokens(conn, data)
        return conn.access_token
    return None


# ---------------------------------------------------------------------------
# OAuth flow
# ---------------------------------------------------------------------------
@login_required(login_url="/login/")
def converty_connect(request):
    """Redirect the seller to Converty's consent page."""
    if not _client_id():
        return HttpResponseBadRequest("CONVERTY_CLIENT_ID non configuré.")
    state = secrets.token_urlsafe(24)
    request.session["converty_oauth_state"] = state
    params = {
        "response_type": "code",
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(request),
        "scope": SCOPES,
        "state": state,
    }
    return HttpResponseRedirect(f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}")


@login_required(login_url="/login/")
def converty_callback(request):
    """Handle the OAuth redirect: validate state, exchange code, store tokens,
    fetch store info, subscribe webhooks."""
    from .models import ConvertyConnection, log_action, AuditLog

    error = request.GET.get("error")
    if error:
        return _simple_page(f"Connexion Converty refusée : {error}")

    code = request.GET.get("code", "")
    state = request.GET.get("state", "")
    saved_state = request.session.pop("converty_oauth_state", None)
    if not code or not state or state != saved_state:
        return _simple_page("État invalide (CSRF) ou code manquant. Réessayez la connexion.")

    status, data = _post_form(TOKEN_URL, {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": _client_id(),
        "client_secret": _client_secret(),
    })
    if status != 200 or not data.get("access_token"):
        return _simple_page(f"Échec de l'échange du code : {data.get('message', 'erreur inconnue')}")

    conn = ConvertyConnection.objects.filter(is_active=True).first() or ConvertyConnection()
    _store_tokens(conn, data)

    # Fetch store info
    st, store = _api_request("GET", "/stores/me", conn.access_token)
    if st == 200 and store.get("success"):
        d = store.get("data", {})
        conn.store_id = d.get("_id", "")
        conn.store_name = d.get("name", "")
        conn.store_currency = d.get("currency", "")
        conn.save()

    # Subscribe webhooks (idempotent — ignore 409 already-subscribed)
    target = request.build_absolute_uri("/webhooks/converty/")
    subscribed = []
    for event in ("order.create", "order.update"):
        sst, sresp = _api_request("POST", "/hooks/subscribe", conn.access_token,
                                  {"targetUrl": target, "event": event})
        if sst in (200, 409):
            subscribed.append(event)

    log_action(
        request.user, AuditLog.OTHER,
        description=f"Converty connecté : store '{conn.store_name}' ({conn.store_currency}), "
                    f"webhooks: {', '.join(subscribed) or 'aucun'}",
        request=request,
    )
    return _simple_page(
        f"✅ Converty connecté : <b>{conn.store_name or conn.store_id}</b><br>"
        f"Webhooks abonnés : {', '.join(subscribed) or 'aucun'}.<br>"
        f"<a href='/sales-orders/'>Retour aux commandes</a>"
    )


def _simple_page(html_body):
    from django.http import HttpResponse
    return HttpResponse(
        f"<html><body style='font-family:sans-serif;padding:40px;max-width:600px;margin:auto;'>"
        f"<h2>Converty</h2><p>{html_body}</p></body></html>"
    )


# ---------------------------------------------------------------------------
# Outbound status push
# ---------------------------------------------------------------------------
def push_status_to_converty(order, our_status):
    """If `order` came from Converty, push the mapped status. Best-effort:
    never raises. Returns (ok: bool, message: str)."""
    try:
        from .models import log_action, AuditLog
        if not getattr(order, "converty_order_id", ""):
            return False, "not a Converty order"
        converty_status = STATUS_MAP.get(our_status)
        if not converty_status:
            return False, f"status {our_status} not mapped"
        token = get_valid_converty_token()
        if not token:
            return False, "no valid Converty token"
        st, resp = _api_request("PATCH", f"/orders/{order.converty_order_id}", token,
                                {"status": converty_status})
        ok = (st == 200 and resp.get("success", True))
        log_action(
            None, AuditLog.EDIT,
            description=f"Converty : commande {order.converty_order_id} → '{converty_status}' "
                        f"(Order v2 #{order.id}) — {'OK' if ok else 'ÉCHEC: ' + str(resp.get('message',''))}",
            target_model="Order", target_id=order.id,
        )
        return ok, resp.get("message", "")
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# Inbound webhook (Stage 2 will add full product matching)
# ---------------------------------------------------------------------------
@csrf_exempt
@require_POST
def api_converty_webhook(request):
    """Receive Converty order.create / order.update webhooks.
    Stage 1: acknowledge and log. Stage 2 adds full order creation/matching.
    """
    from .models import log_action, AuditLog
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"success": False, "message": "Invalid JSON"}, status=400)
    log_action(
        None, AuditLog.OTHER,
        description=f"Webhook Converty reçu (stage 1, non traité) : "
                    f"order _id={payload.get('_id', '?')}, ref={payload.get('reference', '?')}",
    )
    return JsonResponse({"success": True})
