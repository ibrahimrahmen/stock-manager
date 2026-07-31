"""
Rattrapage des commandes Shopify/Converty manquées pendant la fenêtre du bug
(Order.SOURCE_MESSENGER manquant qui faisait planter _create_order_from_shopify_shaped_payload).

Usage sur Railway:
    python manage.py shell < backfill_shopify.py

- Récupère toutes les commandes Shopify depuis CREATED_MIN (pagination complète).
- Pour chacune, saute si elle existe déjà (par shopify_order_id dans les notes).
- Crée les manquantes via _create_order_from_shopify_shaped_payload(source="shopify").
- N'annule/ne modifie jamais une commande existante.
- Affiche un récap: créées / déjà présentes / erreurs.
"""
import json
import urllib.request
import urllib.error
import os
import time

from inventory.models import Order
from inventory.views import _shopify_get_access_token, _create_order_from_shopify_shaped_payload

CREATED_MIN = "2026-07-29T21:00:00Z"   # début de la fenêtre du bug (marge)
PAGE_LIMIT = 250                        # max Shopify par page

domain = os.environ.get("SHOPIFY_SHOP_DOMAIN", "").strip()
tok, err = _shopify_get_access_token()
if not tok:
    print("ERREUR token Shopify:", err)
    raise SystemExit

def _already_exists(shopify_order_id):
    sid = str(shopify_order_id)
    return (Order.objects.filter(notes__contains=f"shopify_order_id={sid}").exists()
            or Order.objects.filter(converty_order_id=sid).exists())

# --- Pagination complète via le header Link (page_info) ---
def _fetch_all_orders():
    all_orders = []
    url = (f"https://{domain}/admin/api/2024-10/orders.json"
           f"?status=any&created_at_min={CREATED_MIN}&limit={PAGE_LIMIT}")
    while url:
        req = urllib.request.Request(url, headers={"X-Shopify-Access-Token": tok})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
            link = resp.headers.get("Link", "") or resp.headers.get("link", "")
        batch = data.get("orders", [])
        all_orders.extend(batch)
        # Trouver le lien "next" dans le header Link
        nxt = ""
        for part in link.split(","):
            if 'rel="next"' in part:
                a = part.find("<"); b = part.find(">")
                if a != -1 and b != -1:
                    nxt = part[a + 1:b]
                break
        url = nxt
        if url:
            time.sleep(0.6)  # respecter le rate limit Shopify
    return all_orders

print("Récupération des commandes Shopify depuis", CREATED_MIN, "...")
orders = _fetch_all_orders()
print("Total commandes Shopify récupérées:", len(orders))

created = 0
skipped = 0
errors = 0
error_list = []

for o in orders:
    sid = str(o.get("id"))
    name = o.get("name") or sid
    if _already_exists(sid):
        skipped += 1
        continue
    try:
        _create_order_from_shopify_shaped_payload(
            o, source="shopify", external_id=sid,
        )
        # Vérifier la création
        if _already_exists(sid):
            created += 1
            print(f"  CRÉÉE  {name} (id {sid})")
        else:
            errors += 1
            error_list.append((name, "créée mais introuvable après"))
            print(f"  ?      {name} (id {sid}) - créée mais non retrouvée")
    except Exception as e:
        errors += 1
        error_list.append((name, str(e)[:120]))
        print(f"  ERREUR {name} (id {sid}): {str(e)[:120]}")

print("\n==== RÉCAP ====")
print("Créées      :", created)
print("Déjà présentes:", skipped)
print("Erreurs     :", errors)
if error_list:
    print("\nDétail erreurs:")
    for nm, msg in error_list[:20]:
        print("  -", nm, "|", msg)
