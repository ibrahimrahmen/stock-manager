"""Backfill: find orders that are 'confirmee' locally but where Navex's
'etat' field already says 'Supprime'. Transition them to the new
SUPPRIME_NAVEX status so they appear in the dedicated filter.

Usage:
    python manage.py fix_supprime_navex_orders             # dry-run (lists only)
    python manage.py fix_supprime_navex_orders --apply     # actually update
"""
from django.core.management.base import BaseCommand
from inventory.models import Order, AuditLog, log_action


class Command(BaseCommand):
    help = "Transition orders that Navex deleted to SUPPRIME_NAVEX status."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually apply the changes. Without this flag, only lists matches.",
        )

    def handle(self, *args, **opts):
        apply_changes = opts.get("apply", False)
        # Find candidates: status=confirmee but Navex says supprime/supprimé/deleted
        candidates = Order.objects.filter(status=Order.CONFIRMEE).exclude(navex_last_status="")
        matches = []
        for o in candidates.iterator():
            s = (o.navex_last_status or "").strip().lower()
            if s in ("supprime", "supprimé", "deleted"):
                matches.append(o)

        self.stdout.write(f"Found {len(matches)} order(s) with Navex status 'Supprime' but local 'Confirmée':")
        for o in matches:
            self.stdout.write(f"  #{o.id}: {o.customer.phone} — bordereau {o.bordereau_barcode} — Navex etat: '{o.navex_last_status}'")

        if not matches:
            self.stdout.write(self.style.SUCCESS("Nothing to do."))
            return

        if not apply_changes:
            self.stdout.write(self.style.WARNING("\nDry-run. Re-run with --apply to actually update these orders."))
            return

        # Apply
        n = 0
        for o in matches:
            o.status = Order.SUPPRIME_NAVEX
            o.save(update_fields=["status", "updated_at"])
            log_action(
                None, AuditLog.STATUS_CHANGE,
                description=f"Backfill: commande #{o.id} passée en 'Supprimé Navex' (Navex etat='{o.navex_last_status}', bordereau {o.bordereau_barcode})",
                target_model="Order", target_id=o.id,
            )
            n += 1
        self.stdout.write(self.style.SUCCESS(f"\n{n} order(s) updated to SUPPRIME_NAVEX."))
