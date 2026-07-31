"""
Rattrapage des commandes Converty manquées pendant la fenêtre du bug
(Order.SOURCE_MESSENGER manquant qui faisait planter la création).

Usage sur Railway:
    python manage.py shell < backfill_converty.py

- Pagine sur l'API Converty (/orders) depuis CREATED_MIN.
- Garde uniquement les états créables (pending/confirmed/uploaded/attempt).
- Pour chaque commande manquante (converty_order_id absent en base), refetch
  la commande complète (pour les couleurs), la transforme via
  _converty_to_shopify_shape et la crée via _create_order_from_shopify_shaped_payload.
- Ne touche jamais une commande existante.
- Récap: créées / déjà présentes / ignorées (état) / erreurs.
"""
import urllib.parse
from datetime import datetime, timezone as _tzutc

from inventory.models import Order
from inventory.converty import get_valid_converty_token, _api_request, _converty_to_shopify_shape
from inventory import views as _views

CREATED_MIN = "2026-07-29T21:00:00Z"   # début fenêtre du bug (marge)
CREATE_STATES = {"pending", "confirmed", "uploaded", "attempt"}
PAGE_SIZE = 50
MAX_PAGES = 40   # garde-fou

def _parse_dt(s):
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except Exception:
        return None

MIN_DT = _parse_dt(CREATED_MIN)

tok = get_valid_converty_token()
if not tok:
    print("ERREUR: pas de token Converty valide")
    raise SystemExit

# --- Récupérer les commandes page par page jusqu'à sortir de la fenêtre ---
def _fetch_window():
    collected = []
    page = 1
    while page <= MAX_PAGES:
        st, data = _api_request("GET", f"/orders?limit={PAGE_SIZE}&page={page}", tok)
        if st != 200 or not isinstance(data, dict):
            print("  arrêt: réponse API", st)
            break
        rows = data.get("data") or []
        if not rows:
            break
        stop = False
        for o in rows:
            dt = _parse_dt(o.get("createdAt"))
            if dt and MIN_DT and dt < MIN_DT:
                stop = True
                break
            collected.append(o)
        if stop:
            break
        page += 1
    return collected

print("Récupération des commandes Converty depuis", CREATED_MIN, "...")
orders = _fetch_window()
print("Commandes dans la fenêtre:", len(orders))

created = 0
skipped_exist = 0
skipped_state = 0
errors = 0
error_list = []

for o in orders:
    cid = str(o.get("_id") or "")
    if not cid:
        continue
    if Order.objects.filter(converty_order_id=cid).exists():
        skipped_exist += 1
        continue
    co_status = (o.get("status") or "").strip().lower()
    if co_status and co_status not in CREATE_STATES:
        skipped_state += 1
        continue
    # Refetch complet (couleurs) via search, comme le webhook.
    full_obj = o
    try:
        key = str(o.get("reference") or cid)
        st_f, data_f = _api_request("GET", f"/orders?search={urllib.parse.quote(key)}", tok)
        rows = data_f.get("data") if isinstance(data_f, dict) else None
        if isinstance(rows, list):
            for row in rows:
                if str(row.get("_id")) == cid:
                    full_obj = row
                    break
    except Exception:
        pass
    try:
        shaped = _converty_to_shopify_shape(full_obj)
        _views._create_order_from_shopify_shaped_payload(
            shaped, source="converty", external_id=cid,
        )
        if Order.objects.filter(converty_order_id=cid).exists():
            created += 1
            print(f"  CRÉÉE  {o.get('reference') or cid} ({co_status})")
        else:
            errors += 1
            error_list.append((cid, "créée mais introuvable après"))
    except Exception as e:
        errors += 1
        error_list.append((cid, str(e)[:120]))
        print(f"  ERREUR {o.get('reference') or cid}: {str(e)[:120]}")

print("\n==== RÉCAP CONVERTY ====")
print("Créées            :", created)
print("Déjà présentes    :", skipped_exist)
print("Ignorées (état)   :", skipped_state)
print("Erreurs           :", errors)
if error_list:
    print("\nDétail erreurs:")
    for cid, msg in error_list[:20]:
        print("  -", cid, "|", msg)
