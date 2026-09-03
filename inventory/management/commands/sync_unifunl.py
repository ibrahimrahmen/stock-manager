"""Pull orders captured by the Unifunl AI chat agent into the system.

Unifunl answers customer chats on Messenger / Instagram / WhatsApp and records
the orders it captures. This command polls Unifunl's read API
(GET /api/v1/public/orders, x-api-key header) and creates any order missing
from our system as a pending (Non confirmée) order for human confirmation.

Idempotent — an order already imported is never created twice.

Requires the environment variable UNIFUNL_API_KEY.

Usage:
    python manage.py sync_unifunl            # dry-run (list what would be created)
    python manage.py sync_unifunl --apply    # create the missing orders
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Import Unifunl orders missing from the system (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually create the missing orders. Without it, only lists them.")

    def handle(self, *args, **opts):
        from inventory.views import _sync_unifunl_orders
        apply_changes = opts.get("apply", False)
        res = _sync_unifunl_orders(apply=apply_changes)

        if res.get("status") != "ok":
            self.stdout.write(self.style.ERROR(res.get("message", "Erreur inconnue")))
            return

        if not apply_changes:
            would = res.get("would_create", [])
            self.stdout.write(
                f"{res.get('fetched', 0)} commande(s) Unifunl récupérée(s) — "
                f"{len(would)} à créer, {res.get('skipped', 0)} déjà présente(s):")
            for w in would[:60]:
                self.stdout.write("  - " + w)
            if not would:
                self.stdout.write(self.style.SUCCESS("Rien à importer — tout est déjà là."))
            else:
                self.stdout.write(self.style.WARNING(
                    "\nDry-run. Relance avec --apply pour créer les commandes manquantes."))
            return

        self.stdout.write(self.style.SUCCESS(
            f"Créées: {res.get('created', 0)} | Déjà présentes: {res.get('skipped', 0)} "
            f"| Erreurs: {res.get('errors', 0)}"))
        for e in res.get("err_list", [])[:20]:
            self.stdout.write("  ERREUR " + e)
