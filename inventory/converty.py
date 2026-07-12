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

BASE = "https://partner.converty.shop"          # OAuth (authorize + token) lives here
AUTHORIZE_URL = f"{BASE}/oauth2/authorize"
TOKEN_URL = f"{BASE}/oauth2/token"
API = "https://api.converty.shop/api/v1"          # API endpoints live on api.* host

SCOPES = "read-orders update-orders read-stores read-hooks create-hooks"

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
    # Force https: Railway terminates SSL at its proxy and forwards http to the
    # app, so build_absolute_uri would otherwise produce an http:// URL that
    # Converty rejects.
    uri = request.build_absolute_uri("/converty/callback/")
    if uri.startswith("http://"):
        uri = "https://" + uri[len("http://"):]
    return uri


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
def _subscribe_webhooks(token, target):
    """Subscribe order.create / order.update. Returns (subscribed, errors_str).
    Converty's docs say POST /hooks/subscribe, but that 404s; the real endpoint
    is the REST collection POST /hooks. We try both for resilience."""
    subscribed = []
    errors = []
    for event in ("order.create", "order.update"):
        body = {"targetUrl": target, "event": event}
        # Try candidate paths in order; accept the first that works.
        ok = False
        last = None
        for path in ("/hooks/subscribe", "/hooks", "/webhooks/subscribe", "/webhooks"):
            sst, sresp = _api_request("POST", path, token, body)
            last = (path, sst, sresp)
            if sst in (200, 201, 409):
                subscribed.append(event)
                ok = True
                break
        if not ok and last:
            errors.append(f"{event}: {last[0]} http={last[1]} {str(last[2].get('message', last[2]))[:120]}")
    return subscribed, "; ".join(errors)


@login_required(login_url="/login/")
def converty_resubscribe(request):
    """Manually (re)subscribe the Converty webhooks and show the result.
    Useful when the initial subscribe failed during OAuth."""
    from .models import ConvertyConnection, log_action, AuditLog
    token = get_valid_converty_token()
    if not token:
        return _simple_page("Pas de connexion Converty active. Connectez d'abord la boutique.")
    target = request.build_absolute_uri("/webhooks/converty/")
    if target.startswith("http://"):
        target = "https://" + target[len("http://"):]
    # First, list existing hooks for visibility
    lst_status, lst = _api_request("GET", "/hooks", token)
    existing = lst.get("data", []) if isinstance(lst, dict) else []
    subscribed, errors = _subscribe_webhooks(token, target)
    log_action(
        request.user, AuditLog.OTHER,
        description=f"Converty webhooks (manuel) : abonnés={subscribed}, erreurs={errors or 'aucune'}, "
                    f"existants={len(existing)}, target={target}",
        request=request,
    )
    return _simple_page(
        f"Webhooks Converty<br>"
        f"Target : <code>{target}</code><br>"
        f"Abonnés : {', '.join(subscribed) or 'aucun'}<br>"
        f"Erreurs : {errors or 'aucune'}<br>"
        f"Hooks existants côté Converty : {len(existing)}<br>"
        f"<a href='/admin-tools/'>Retour</a>"
    )


@login_required(login_url="/login/")
def converty_connect(request):
    """Redirect the seller to Converty's consent page."""
    if not _client_id():
        return HttpResponseBadRequest("CONVERTY_CLIENT_ID non configuré.")
    # Stateless CSRF state: a signed token (no session dependency, which can be
    # lost across the external redirect). Verified by signature on callback.
    from django.core import signing
    state = signing.dumps({"u": request.user.id}, salt="converty-oauth")
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
    # Verify the signed state (valid for 1 hour). No session dependency.
    from django.core import signing
    state_ok = False
    if state:
        try:
            signing.loads(state, salt="converty-oauth", max_age=3600)
            state_ok = True
        except signing.BadSignature:
            state_ok = False
    if not code or not state_ok:
        return _simple_page("État invalide (CSRF) ou code manquant. Réessayez la connexion.")

    status, data = _post_form(TOKEN_URL, {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": _client_id(),
        "client_secret": _client_secret(),
        "redirect_uri": _redirect_uri(request),
    })
    if status != 200 or not data.get("access_token"):
        try:
            log_action(
                request.user, AuditLog.OTHER,
                description=f"Converty token exchange ÉCHEC : http={status}, resp={str(data)[:300]}",
                request=request,
            )
        except Exception:
            pass
        return _simple_page(
            f"Échec de l'échange du code (HTTP {status}) : "
            f"{data.get('message', str(data)[:200] or 'erreur inconnue')}"
        )

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
    if target.startswith("http://"):
        target = "https://" + target[len("http://"):]
    subscribed, sub_errors = _subscribe_webhooks(conn.access_token, target)
    if sub_errors:
        try:
            log_action(
                request.user, AuditLog.OTHER,
                description=f"Converty webhooks ÉCHEC : {sub_errors}",
                request=request,
            )
        except Exception:
            pass

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
# Inbound webhook — translate Converty payload to Shopify-shape, reuse engine
# ---------------------------------------------------------------------------
def _converty_to_shopify_shape(co):
    """Map a Converty order object to the Shopify-shaped payload the shared
    order-creation engine expects."""
    cust = co.get("customer") or {}
    # Address: Converty has address + town/city; combine for our address field.
    addr_parts = [p for p in (cust.get("address"), cust.get("town")) if p]
    address1 = ", ".join(addr_parts) if addr_parts else (cust.get("address") or "")
    shipping = {
        "first_name": cust.get("name") or "",
        "last_name": "",
        "phone": cust.get("phone") or "",
        "address1": address1,
        "address2": "",
        "city": cust.get("city") or "",
        "province": cust.get("city") or "",
    }

    line_items = []
    for item in (co.get("cart") or []):
        prod = item.get("product")
        # Converty sends `product` either as an object {"name": ...} or, in newer
        # payloads, as a plain string (e.g. "pull ami"). Handle both, and fall
        # back to the top-level SKU as the name if needed.
        if isinstance(prod, dict):
            name = prod.get("name") or ""
        elif isinstance(prod, str):
            name = prod
            prod = {}
        else:
            name = ""
            prod = {}
        if not name:
            name = (item.get("sku") or "").strip()
        # Build a Shopify-style variant_title from selectedVariants values
        # (e.g. [{"name":"Size","value":"M"}] -> "M"; size+color -> "M / Bleu").
        sv_values = []
        sel_color_val = ""   # selected COULEUR value (image url or hex)
        sel_size_val = ""    # selected size
        for sv in (item.get("selectedVariants") or []):
            nm = (sv.get("name") or "").strip().lower()
            val = (sv.get("value") or "").strip()
            if val:
                sv_values.append(val)
            if nm in ("couleur", "color", "colour"):
                sel_color_val = val
            elif nm in ("taille", "size"):
                sel_size_val = val
        variant_title = " / ".join(sv_values)
        # The selected COULEUR is often an image URL — useless and pollutes the
        # colour parser. Rebuild variant_title from the SIZE only; the real
        # colour comes from the variant SKU resolved below.
        if sel_color_val and sel_color_val in variant_title:
            variant_title = sel_size_val or ""
        # Also expose each selectedVariant as a Shopify "property" so the
        # size/color extractors (which read name=size/couleur) can use them.
        properties = []
        for sv in (item.get("selectedVariants") or []):
            properties.append({"name": sv.get("name") or "", "value": sv.get("value") or ""})

        # Converty colour resolution: the PER-VARIANT SKU holds the colour name
        # (e.g. "BLANC", "NOIR", "NOIR/BLANC"). Each variant in newVariants has
        # selectedValues=[colorImageUrl, size]. Match the order's selected colour
        # (+ size) to its variant, then use THAT variant's SKU as the colour.
        color_sku = ""
        new_variants = prod.get("newVariants") or []
        if new_variants and (sel_color_val or sel_size_val):
            best = None
            for v in new_variants:
                vals = v.get("selectedValues") or []
                v_color = str(vals[0]) if len(vals) >= 1 else ""
                v_size = str(vals[1]) if len(vals) >= 2 else ""
                color_ok = (not sel_color_val) or (v_color == sel_color_val)
                size_ok = (not sel_size_val) or (v_size == sel_size_val)
                if color_ok and size_ok:
                    best = v
                    break
            # If size didn't line up, match colour only.
            if best is None and sel_color_val:
                for v in new_variants:
                    vals = v.get("selectedValues") or []
                    if vals and str(vals[0]) == sel_color_val:
                        best = v
                        break
            if best is not None:
                color_sku = (best.get("sku") or "").strip()

        # The colour SKU (BLANC / NOIR / NOIR/BLANC) is the colour source. Expose
        # it as a "couleur" property AND fold into variant_title so the positional
        # colour extractor splits "NOIR/BLANC" across the offer's sub-products.
        if color_sku:
            properties.append({"name": "couleur", "value": color_sku})
            variant_title = (variant_title + " / " + color_sku).strip(" /") if variant_title else color_sku
        line_items.append({
            "title": name,
            "name": name,
            "variant_title": variant_title,
            "properties": properties,
            "quantity": int(item.get("quantity") or 1),
            "price": str(item.get("pricePerUnit") or prod.get("price") or "0"),
            "sku": color_sku or (prod.get("sku") or ""),
        })

    total = co.get("total") or {}
    delivery = total.get("deliveryPrice")
    shipping_lines = []
    if delivery:
        shipping_lines = [{"price": str(delivery)}]

    return {
        "id": co.get("_id") or "",
        "order_number": co.get("reference") or "",
        "name": str(co.get("reference") or ""),
        "shipping_address": shipping,
        "billing_address": shipping,
        "customer": {"phone": cust.get("phone") or "", "first_name": cust.get("name") or ""},
        "phone": cust.get("phone") or "",
        "note": cust.get("note") or co.get("note") or "",
        "line_items": line_items,
        "shipping_lines": shipping_lines,
    }


@csrf_exempt
@require_POST
def api_converty_webhook(request):
    """Receive Converty order.create / order.update webhooks and create a v2
    Order using the shared matching engine. Dedups on converty_order_id."""
    from .models import log_action, AuditLog
    from . import views as _views
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"success": False, "message": "Invalid JSON"}, status=400)

    # The webhook may wrap the order under "data" or send it directly.
    order_obj = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    converty_id = str(order_obj.get("_id") or "")
    # Log every arrival so we can confirm Converty is calling us.
    try:
        cart_dbg = []
        for it in (order_obj.get("cart") or []):
            prod = it.get("product") or {}
            cart_dbg.append({
                "product": prod.get("name"),
                "sku": prod.get("sku"),
                "variants": prod.get("variants"),
                "selectedVariants": it.get("selectedVariants"),
            })
        log_action(
            None, AuditLog.OTHER,
            description=f"Webhook Converty REÇU : _id={converty_id or '?'}, "
                        f"ref={order_obj.get('reference', '?')}, status={order_obj.get('status', '?')}, "
                        f"cart={json.dumps(cart_dbg, ensure_ascii=False)[:600]}",
        )
    except Exception:
        pass
    if not converty_id:
        return JsonResponse({"success": True, "message": "No order id, ignored."})

    # If we already imported this order, don't create a duplicate. (We could
    # update it here later, but for now we just acknowledge.)
    from .models import Order
    if Order.objects.filter(converty_order_id=converty_id).exists():
        return JsonResponse({"success": True, "message": "already imported"})

    # Only create genuinely new, active orders. Webhooks also fire on
    # order.update for OLD orders (e.g. when rejected/edited) — we must not
    # pull those in as fresh orders. Accept only incoming/active states.
    co_status = (order_obj.get("status") or "").strip().lower()
    CREATE_STATES = {"pending", "confirmed", "uploaded", "attempt"}
    if co_status and co_status not in CREATE_STATES:
        return JsonResponse({"success": True, "message": f"status '{co_status}' ignored"})

    # The webhook payload's selectedVariants carry the COULEUR as an IMAGE URL
    # (not usable for color). The full order from the API carries the per-variant
    # data (newVariants with colour SKUs). Direct GET /orders/<id> returns 404 on
    # this API, but GET /orders?search=<id> returns the single matching order.
    # Fetch it so we can resolve the colour; fall back to the webhook payload.
    full_obj = order_obj
    try:
        token = get_valid_converty_token()
        if token:
            import urllib.parse as _up
            fetched = None
            for _key in (str(order_obj.get("reference") or ""), converty_id):
                if not _key:
                    continue
                st_f, data_f = _api_request(
                    "GET", f"/orders?search={_up.quote(str(_key))}", token
                )
                rows = data_f.get("data") if isinstance(data_f, dict) else None
                if isinstance(rows, list):
                    # Prefer an exact _id match; else take the single result.
                    for row in rows:
                        if str(row.get("_id")) == str(converty_id):
                            fetched = row
                            break
                    if fetched is None and len(rows) == 1:
                        fetched = rows[0]
                if fetched:
                    break
            if fetched and (fetched.get("cart") or fetched.get("_id")):
                full_obj = fetched
    except Exception:
        pass

    # Block orders flagged NOFILL: if any cart item's SKU is "NOFILL" (case-
    # insensitive), do not import the order at all. Used to keep placeholder /
    # non-fulfillable products out of the system.
    def _has_nofill(obj):
        for it in (obj.get("cart") or []):
            skus = []
            prod = it.get("product")
            if isinstance(prod, dict):
                skus.append(prod.get("sku") or "")
            skus.append(it.get("sku") or "")
            for v in (it.get("selectedVariants") or []):
                skus.append(v.get("sku") or "")
            if any(str(s).strip().upper() == "NOFILL" for s in skus):
                return True
        return False

    if _has_nofill(full_obj) or _has_nofill(order_obj):
        log_action(
            None, AuditLog.OTHER,
            description=f"Converty _id={converty_id} IGNORÉ : SKU NOFILL (non importé)",
        )
        return JsonResponse({"success": True, "message": "NOFILL — ignored"})

    # Only create from confirmed-and-earlier states; ignore terminal Converty
    # states we don't want to import as fresh orders.
    shaped = _converty_to_shopify_shape(full_obj)
    try:
        resp = _views._create_order_from_shopify_shaped_payload(
            shaped, source="converty", external_id=converty_id, request=request,
        )
        return JsonResponse({"success": True})
    except Exception as e:
        log_action(
            None, AuditLog.OTHER,
            description=f"Webhook Converty ERREUR pour _id={converty_id} : {str(e)[:300]}",
        )
        # Acknowledge so Converty doesn't hammer retries; we logged it.
        return JsonResponse({"success": True, "message": "logged error"})
