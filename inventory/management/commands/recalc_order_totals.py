"""Recompute total for all Orders.

Use this one-shot command to fix orders that were saved with total=0 due to
a bug where Order.total wasn't recalculated after autosave.

Usage:
    python manage.py recalc_order_totals
    python manage.py recalc_order_totals --status non_confirmee  # only drafts
"""
from django.core.management.base import BaseCommand
from inventory.models import Order


class Command(BaseCommand):
    help = "Recompute Order.total for all (or filtered) orders."

    def add_arguments(self, parser):
        parser.add_argument(
            "--status",
            help="Optionally filter by order status (e.g. 'non_confirmee').",
            default=None,
        )

    def handle(self, *args, **opts):
        qs = Order.objects.all()
        if opts.get("status"):
            qs = qs.filter(status=opts["status"])
        total = qs.count()
        self.stdout.write(f"Recomputing total for {total} order(s)…")
        fixed = 0
        for i, order in enumerate(qs.iterator(), 1):
            old_total = order.total
            order.recalc_total()
            order.refresh_from_db()
            if order.total != old_total:
                fixed += 1
                self.stdout.write(
                    f"  #{order.id}: {old_total} → {order.total} DT"
                )
            if i % 50 == 0:
                self.stdout.write(f"  ({i}/{total})")
        self.stdout.write(self.style.SUCCESS(f"Done. {fixed} order(s) had their total updated."))
