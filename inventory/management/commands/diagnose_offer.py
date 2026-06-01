from django.core.management.base import BaseCommand
from inventory.models import Offer, Product, ProductVariant, ProductUnit
from django.db.models import Q


class Command(BaseCommand):
    help = "Read-only: trace an offer's products, variants, and unit counts by size."

    def add_arguments(self, parser):
        parser.add_argument("--offer-id", type=int, help="Offer ID to inspect")
        parser.add_argument("--name", type=str, help="Offer name (or part) to search")

    def handle(self, *args, **opts):
        qs = Offer.objects.all()
        if opts.get("offer_id"):
            qs = qs.filter(id=opts["offer_id"])
        elif opts.get("name"):
            qs = qs.filter(name__icontains=opts["name"])
        else:
            self.stdout.write("Pass --offer-id or --name")
            return

        for offer in qs:
            self.stdout.write(f"\n=== OFFER #{offer.id}: {offer.name} (active={offer.is_active}) ===")
            ops = offer.products.all()
            if not ops:
                self.stdout.write("  (offer has NO products linked)")
            for op in ops:
                p = op.product
                root = p.parent_product or p
                family = Product.objects.filter(Q(id=root.id) | Q(parent_product=root))
                self.stdout.write(
                    f"  OfferProduct -> product #{p.id} '{p.name}' "
                    f"(parent={p.parent_product_id}) qty={op.quantity}"
                )
                self.stdout.write(f"    root=#{root.id} '{root.name}'  family size={family.count()}")
                for fam_p in family:
                    variants = fam_p.variants.all()
                    self.stdout.write(
                        f"      product #{fam_p.id} '{fam_p.name}' -> {variants.count()} variant(s)"
                    )
                    for v in variants:
                        units = v.units.all()
                        by_size = {}
                        for u in units:
                            by_size[u.size] = by_size.get(u.size, 0) + 1
                        self.stdout.write(
                            f"        variant #{v.id} color='{v.color_label or v.color_name}' "
                            f"units={units.count()} by_size={by_size}"
                        )
        self.stdout.write("\n(done — read-only, nothing changed)")
