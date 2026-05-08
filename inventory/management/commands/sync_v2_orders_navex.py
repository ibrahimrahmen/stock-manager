"""Hourly sync of Navex statuses for v2 orders.

Run by Railway cron (or any scheduler) every hour:
    python manage.py sync_v2_orders_navex
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Refresh Navex status for v2 orders that have a bordereau"

    def add_arguments(self, parser):
        parser.add_argument(
            "--all", action="store_true",
            help="Sync ALL orders, even those already in terminal states (slower).",
        )

    def handle(self, *args, **opts):
        from inventory.views import _sync_navex_for_v2_orders
        from inventory.models import log_action, AuditLog
        only_pending = not opts.get("all")
        n_attempted, n_updated = _sync_navex_for_v2_orders(only_pending=only_pending)
        self.stdout.write(self.style.SUCCESS(
            f"[sync_v2_orders_navex] attempted={n_attempted} updated={n_updated}"
        ))
        # Audit log row, attributed to "system" (no user)
        try:
            from inventory.models import AuditLog as _AL
            _AL.objects.create(
                user=None,
                username="system_cron",
                action=_AL.NAVEX_SYNC,
                description=f"Sync auto Navex v2 (cron): {n_updated}/{n_attempted} mis à jour",
            )
        except Exception:
            pass
