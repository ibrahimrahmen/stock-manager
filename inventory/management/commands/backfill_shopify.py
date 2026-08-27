"""Re-import missed Shopify orders (idempotent).

Pulls orders from the Shopify Admin API since a date and creates any that are
NOT already in the system. Useful after a webhook-delivery gap — e.g. the
database was full and couldn't save incoming website orders, so Shopify's
webhook failed and those orders never landed here (even though the purchase
happened and Meta recorded it).

Never modifies or cancels an existing order — it only creates the missing ones.

Usage:
    python manage.py backfill_shopify                       # dry-run (list only)
    python manage.py backfill_shopify --apply               # create missing
    python manage.py backfill_shopify --since 2026-08-20T00:00:00Z --apply
"""
import json
import os
import time
import urllib.request
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from inventory.models import Order


class Command(BaseCommand):
    help = "Re-import Shopify orders missing from the system (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--since", default="",
            help="ISO datetime (e.g. 2026-08-20T00:00:00Z). Default: 14 days ago.")
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually create the missing orders. Without it, only lists them.")

    def _exists(self, sid):
        sid = str(sid)
        return (Order.objects.filter(notes__contains=f"shopify_order_id={sid}").exists()
                or Order.objects.filter(converty_order_id=sid).exists())

    def handle(self, *args, **opts):
        from inventory.views import (_shopify_get_access_token,
                                     _create_order_from_shopify_shaped_payload)
        domain = os.environ.get("SHOPIFY_SHOP_DOMAIN", "").strip()
        tok, err = _shopify_get_access_token()
        if not domain or not tok:
            self.stdout.write(self.style.ERROR(
                f"Shopify indisponible (domaine/token): {err or 'SHOPIFY_SHOP_DOMAIN manquant'}"))
            return
        since = opts.get("since") or (
            timezone.now() - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
        apply_changes = opts.get("apply", False)

        # Fetch all orders since `since`, following Shopify's Link pagination.
        orders = []
        url = (f"https://{domain}/admin/api/2024-10/orders.json"
               f"?status=any&created_at_min={since}&limit=250")
        try:
            while url:
                req = urllib.request.Request(url, headers={"X-Shopify-Access-Token": tok})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.load(resp)
                    link = resp.headers.get("Link", "") or resp.headers.get("link", "")
                orders.extend(data.get("orders", []))
                nxt = ""
                for part in link.split(","):
                    if 'rel="next"' in part:
                        a, b = part.find("<"), part.find(">")
                        if a != -1 and b != -1:
                            nxt = part[a + 1:b]
                        break
                url = nxt
                if url:
                    time.sleep(0.6)  # respect Shopify rate limit
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Erreur API Shopify: {str(e)[:200]}"))
            return

        missing = [o for o in orders if not self._exists(o.get("id"))]
        self.stdout.write(
            f"{len(orders)} commande(s) Shopify depuis {since} — "
            f"{len(missing)} manquante(s) dans le système:")
        for o in missing[:60]:
            self.stdout.write(
                f"  - {o.get('name') or o.get('id')} (id {o.get('id')}, {o.get('created_at')})")

        if not missing:
            self.stdout.write(self.style.SUCCESS("Rien à rattraper — tout est déjà là."))
            return
        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                "\nDry-run. Relance avec --apply pour créer les commandes manquantes."))
            return

        created = errors = 0
        for o in missing:
            sid = str(o.get("id"))
            try:
                _create_order_from_shopify_shaped_payload(o, source="shopify", external_id=sid)
                if self._exists(sid):
                    created += 1
                    self.stdout.write(f"  CRÉÉE  {o.get('name') or sid}")
                else:
                    errors += 1
                    self.stdout.write(f"  ?      {o.get('name') or sid} — créée mais introuvable")
            except Exception as e:
                errors += 1
                self.stdout.write(f"  ERREUR {o.get('name') or sid}: {str(e)[:150]}")
        self.stdout.write(self.style.SUCCESS(f"\nCréées: {created} | Erreurs: {errors}"))
