import re
from django.core.management.base import BaseCommand
from inventory.models import Product


# Suffixes that mark a child version of a base product.
# "Shirt Icy Maze #2" -> base "Shirt Icy Maze"; "Pants ICY MAZE V2" -> "Pants ICY MAZE"
SUFFIX_RE = re.compile(r"\s*(#\s*\d+|v\s*\d+|version\s*\d+)\s*$", re.IGNORECASE)


def base_name(name):
    """Strip a trailing version marker (#2, V2, version 3...) from a name."""
    return SUFFIX_RE.sub("", name or "").strip()


class Command(BaseCommand):
    help = ("Detect child products (name ends with #2 / V2 / version N) and link "
            "them to their base product via parent_product. Dry-run by default; "
            "pass --apply to actually set the links.")

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Actually set parent_product links (otherwise dry-run).")

    def handle(self, *args, **opts):
        apply = opts["apply"]
        products = list(Product.objects.all())
        # Index base-name (lowercased) -> list of candidate parent products
        by_norm = {}
        for p in products:
            by_norm.setdefault(p.name.strip().lower(), []).append(p)

        linked = 0
        ambiguous = 0
        already = 0
        no_base = 0

        for child in products:
            stripped = base_name(child.name)
            if stripped.lower() == child.name.strip().lower():
                continue  # no version suffix -> not a child
            if child.parent_product_id:
                already += 1
                continue
            candidates = by_norm.get(stripped.lower(), [])
            # Exclude self and other versioned names
            candidates = [c for c in candidates if c.id != child.id]
            if not candidates:
                self.stdout.write(self.style.WARNING(
                    f"  '{child.name}' (#{child.id}): no base product '{stripped}' found"
                ))
                no_base += 1
                continue
            if len(candidates) > 1:
                self.stdout.write(self.style.WARNING(
                    f"  '{child.name}' (#{child.id}): AMBIGUOUS base -> "
                    f"{[f'{c.name}#{c.id}' for c in candidates]}"
                ))
                ambiguous += 1
                continue
            parent = candidates[0]
            # Guard against linking a product to itself or to another child.
            if parent.parent_product_id:
                parent = parent.parent_product  # point to the true root
            self.stdout.write(
                f"  LINK '{child.name}' (#{child.id}) -> parent '{parent.name}' (#{parent.id})"
            )
            if apply:
                child.parent_product = parent
                child.save(update_fields=["parent_product"])
            linked += 1

        verb = "Linked" if apply else "Would link"
        self.stdout.write(self.style.SUCCESS(
            f"\n{verb}: {linked} | already linked: {already} | "
            f"ambiguous: {ambiguous} | no base found: {no_base}"
        ))
        if not apply:
            self.stdout.write("Dry-run only. Re-run with --apply to set the links.")
