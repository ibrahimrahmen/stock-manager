"""Backfill: transition orders that Navex marks as 'Livré' to our local
LIVREE status. Useful right after deploying the LIVREE status, to catch up
all orders that were already delivered before.

Usage:
    python manage.py fix_livree_orders             # dry-run (lists only)
    python manage.py fix_livree_orders --apply     # actually apply
"""
from django.core.management.base import BaseCommand
from inventory.models import Order, AuditLog, log_action


DELIVERED_STATES = (
    "livre", "livré", "livree", "livrée",
    "livrer", "livrer paye", "livré payé", "livre paye", "livre payé",
)


class Command(BaseCommand):
    help = "Transition orders Navex says are 'Livré' to LIVREE status."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually apply the changes. Without this flag, only lists matches.",
        )

    def handle(self, *args, **opts):
        apply_changes = opts.get("apply", False)
        candidates = Order.objects.filter(status=Order.CONFIRMEE).exclude(navex_last_status="")
        matches = []
        for o in candidates.iterator():
            s = (o.navex_last_status or "").strip().lower()
            if s in DELIVERED_STATES:
                matches.append(o)

        self.stdout.write(f"Found {len(matches)} order(s) confirmées with Navex='Livré' but local status still 'Confirmée':")
        for o in matches:
            self.stdout.write(f"  #{o.id}: {o.customer.phone} — bordereau {o.bordereau_barcode} — Navex etat: '{o.navex_last_status}'")

        if not matches:
            self.stdout.write(self.style.SUCCESS("Nothing to do."))
            return

        if not apply_changes:
            self.stdout.write(self.style.WARNING("\nDry-run. Re-run with --apply to actually update these orders."))
            return

        n = 0
        for o in matches:
            o.status = Order.LIVREE
            o.save(update_fields=["status", "updated_at"])
            log_action(
                None, AuditLog.STATUS_CHANGE,
                description=f"Backfill: commande #{o.id} passée en 'Livrée' (Navex etat='{o.navex_last_status}', bordereau {o.bordereau_barcode})",
                target_model="Order", target_id=o.id,
            )
            n += 1
        self.stdout.write(self.style.SUCCESS(f"\n{n} order(s) updated to LIVREE."))
