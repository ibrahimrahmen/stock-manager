"""
One-off / repeatable fix: relink ExchangeReturnItem rows that have unit=None
to the REAL physical unit from the original delivered order, so scanning the
code échange shows the actual barcode (e.g. PRC2-BLU-5-016) instead of the
RETURN-<id> placeholder.

Matching is layered, identical to the live create logic:
  1) exact variant + size
  2) same variant, any size
  3) same colour across product versions (V1/V2 variant ids differ)

Usage (Railway / cmd.exe):
  python manage.py relink_exchange_return_units
  python manage.py relink_exchange_return_units --order 158
  python manage.py relink_exchange_return_units --dry-run
"""
from django.core.management.base import BaseCommand
from inventory.models import ExchangeReturnItem


class Command(BaseCommand):
    help = "Relink exchange return items (unit=None) to real delivered units."

    def add_arguments(self, parser):
        parser.add_argument("--order", type=int, default=None,
                            help="Only process this exchange Order id.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Show what would change without saving.")

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        order_id = opts["order"]

        qs = ExchangeReturnItem.objects.filter(unit__isnull=True).select_related(
            "exchange_order__exchange_of", "variant"
        )
        if order_id:
            qs = qs.filter(exchange_order_id=order_id)

        # Group return items by their exchange order so claimed units don't
        # get assigned twice within the same order.
        by_order = {}
        for ri in qs:
            by_order.setdefault(ri.exchange_order_id, []).append(ri)

        total_fixed = 0
        total_unmatched = 0

        for exchange_order_id, items in by_order.items():
            exchange = items[0].exchange_order
            original = exchange.exchange_of
            if original is None:
                self.stdout.write(self.style.WARNING(
                    f"Exchange #{exchange_order_id}: no source order — skipped ({len(items)} item(s))."
                ))
                total_unmatched += len(items)
                continue

            # Build unit pools from the original delivered order.
            units_by_key = {}
            units_by_variant = {}
            units_by_color = {}
            for so in original.shipping_orders.all():
                for oi in so.items.select_related("unit__variant").all():
                    u = oi.unit
                    if not u:
                        continue
                    units_by_key.setdefault((u.variant_id, str(u.size or "")), []).append(u)
                    units_by_variant.setdefault(u.variant_id, []).append(u)
                    clbl = ((u.variant.color_label if u.variant else "")
                            or (u.variant.color_name if u.variant else "")).strip().lower()
                    if clbl:
                        units_by_color.setdefault(clbl, []).append(u)

            claimed = set(
                exchange.return_items.exclude(unit__isnull=True)
                .values_list("unit_id", flat=True)
            )

            def take(variant, size):
                size_str = str(size or "")
                pools = [
                    units_by_key.get((variant.id, size_str), []),
                    units_by_variant.get(variant.id, []),
                ]
                clbl = ((variant.color_label or variant.color_name or "").strip().lower())
                if clbl:
                    pools.append(units_by_color.get(clbl, []))
                for pool in pools:
                    for u in pool:
                        if u.id not in claimed:
                            claimed.add(u.id)
                            return u
                return None

            for ri in items:
                if ri.variant is None:
                    total_unmatched += 1
                    continue
                u = take(ri.variant, ri.size)
                if u is None:
                    self.stdout.write(self.style.WARNING(
                        f"  RETURN-{ri.id} (exch #{exchange_order_id}): no free unit for "
                        f"{ri.variant} / {ri.size} — left unlinked."
                    ))
                    total_unmatched += 1
                    continue
                self.stdout.write(
                    f"  RETURN-{ri.id} (exch #{exchange_order_id}) -> {u.barcode}"
                    + ("  [dry-run]" if dry else "")
                )
                if not dry:
                    ri.unit = u
                    if not ri.size and u.size:
                        ri.size = str(u.size)
                    ri.save(update_fields=["unit", "size"])
                total_fixed += 1

        style = self.style.SUCCESS if not dry else self.style.NOTICE
        self.stdout.write(style(
            f"Done. Linked: {total_fixed}. Unmatched: {total_unmatched}."
            + (" (dry-run — nothing saved)" if dry else "")
        ))
