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
        from inventory.models import Product
        from django.db.models import Q
        dry = opts["dry_run"]
        items = ExchangeReturnItem.objects.filter(unit__isnull=True).select_related(
            "exchange_order__exchange_of", "variant__product"
        )
        fixed = 0
        skipped = 0

        def color_keys(variant):
            return {
                (variant.color_name or "").strip().lower(),
                (variant.color_label or "").strip().lower(),
            } - {""}

        for ri in items:
            exchange = ri.exchange_order
            original = exchange.exchange_of if exchange else None
            if original is None or ri.variant is None:
                skipped += 1
                continue

            # The order line references the PARENT product, but the unit shipped
            # may be a V2/V3 child (same SKU family). Match on family + color + size.
            ri_family_ids = set(
                ri.variant.product.family_products().values_list("id", flat=True)
            )
            ri_colors = color_keys(ri.variant)
            ri_size = str(ri.size)

            claimed = set(
                exchange.return_items.exclude(unit__isnull=True)
                .values_list("unit_id", flat=True)
            )

            matched = None
            for so in original.shipping_orders.all():
                for oi in so.items.select_related("unit__variant__product").all():
                    u = oi.unit
                    if not u or u.id in claimed:
                        continue
                    uv = u.variant
                    if uv is None:
                        continue
                    # same SKU family?
                    if uv.product_id not in ri_family_ids:
                        u_family_ids = set(
                            uv.product.family_products().values_list("id", flat=True)
                        )
                        if not (ri_family_ids & u_family_ids):
                            continue
                    # same color?
                    if ri_colors and not (ri_colors & color_keys(uv)):
                        continue
                    # same size?
                    if str(u.size) != ri_size:
                        continue
                    matched = u
                    break
                if matched:
                    break

            # Fallback: size on the order may have been recorded wrong vs what
            # was physically shipped. If family + color matches EXACTLY ONE
            # unclaimed unit (regardless of size), it's unambiguous — link it.
            if matched is None:
                fam_color_units = []
                for so in original.shipping_orders.all():
                    for oi in so.items.select_related("unit__variant__product").all():
                        u = oi.unit
                        if not u or u.id in claimed:
                            continue
                        uv = u.variant
                        if uv is None:
                            continue
                        in_family = uv.product_id in ri_family_ids
                        if not in_family:
                            u_family_ids = set(
                                uv.product.family_products().values_list("id", flat=True)
                            )
                            in_family = bool(ri_family_ids & u_family_ids)
                        if not in_family:
                            continue
                        if ri_colors and not (ri_colors & color_keys(uv)):
                            continue
                        fam_color_units.append(u)
                if len(fam_color_units) == 1:
                    matched = fam_color_units[0]
                    self.stdout.write(
                        f"  (size mismatch, single unit) RI#{ri.id} -> {matched.barcode}"
                    )

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
