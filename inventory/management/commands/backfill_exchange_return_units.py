from django.core.management.base import BaseCommand
from inventory.models import ExchangeReturnItem


class Command(BaseCommand):
    help = ("Link ExchangeReturnItems that have no unit to the matching physical "
            "unit from their exchange's original delivered order (by variant+size). "
            "Fixes old exchanges that show RETURN-<id> placeholders instead of the "
            "real barcode.")

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Show what would change without saving.")

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        items = ExchangeReturnItem.objects.filter(unit__isnull=True).select_related(
            "exchange_order__exchange_of", "variant"
        )
        fixed = 0
        skipped = 0
        for ri in items:
            exchange = ri.exchange_order
            original = exchange.exchange_of if exchange else None
            if original is None:
                skipped += 1
                continue
            # Gather units already linked to other return items of THIS exchange,
            # so we don't link two return rows to the same physical unit.
            claimed = set(
                exchange.return_items.exclude(unit__isnull=True)
                .values_list("unit_id", flat=True)
            )
            matched = None
            for so in original.shipping_orders.all():
                for oi in so.items.select_related("unit__variant").all():
                    u = oi.unit
                    if (u and u.id not in claimed
                            and u.variant_id == ri.variant_id
                            and str(u.size) == str(ri.size)):
                        matched = u
                        break
                if matched:
                    break
            if matched:
                self.stdout.write(
                    f"ExchangeReturnItem #{ri.id} (exch #{exchange.id}) -> {matched.barcode}"
                )
                if not dry:
                    ri.unit = matched
                    ri.save(update_fields=["unit"])
                fixed += 1
            else:
                self.stdout.write(self.style.WARNING(
                    f"ExchangeReturnItem #{ri.id} (exch #{exchange.id}): no matching unit found"
                ))
                skipped += 1

        verb = "Would fix" if dry else "Fixed"
        self.stdout.write(self.style.SUCCESS(
            f"\n{verb} {fixed} item(s), {skipped} skipped."
        ))
