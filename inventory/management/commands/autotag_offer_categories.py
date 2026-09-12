"""Auto-tag each Offer's category from keywords in its name.

Only fills offers whose category is still blank (never overwrites a category a
human already set). A first pass — staff can correct any it gets wrong.

Usage:
    python manage.py autotag_offer_categories            # dry-run (lists only)
    python manage.py autotag_offer_categories --apply    # actually set them
"""
from django.core.management.base import BaseCommand
from inventory.models import Offer


# Ordered most-specific first. The first category whose keyword appears in the
# offer name (lowercased) wins.
RULES = [
    ("ensemble",   ["ensemble", "tenue", "3pcs", "2pcs", "3 pcs", "2 pcs",
                    "3p ", "2p ", "3 pieces", "2 pieces", "pack", "survet", "survêt"]),
    ("hoodie",     ["hoodie", "capuche", "sweat a capuche", "sweat à capuche"]),
    # Shoes BEFORE veste: "Claquette Cuir" must be claquette, not veste ("cuir").
    ("claquette",  ["claquette", "sandale", "slide"]),
    ("espadrille", ["espadrille", "chaussure", "sneaker", "basket", "shoe",
                    "running", "air max", "air force"]),
    ("veste",      ["veste", "bombers", "bomber", "manteau", "gilet", "jacket", "cuir", "doudoune"]),
    ("pantalon",   ["pantalon", "pants", "jogging", "short", "cargo", "jean"]),
    ("sport",      ["sport", "training", "maillot", "jersey", "legging"]),
    ("pull",       ["pull", "polo", "t-shirt", "tshirt", "t shirt", "tee", "chemise", "sweat", "top", "haut"]),
]


def _guess_category(name):
    n = (name or "").lower()
    for cat, kws in RULES:
        if any(k in n for k in kws):
            return cat
    return ""


class Command(BaseCommand):
    help = "Set Offer.category from the offer name (only where blank)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually set categories. Without this flag, only lists guesses.",
        )

    def _guess_season(self, offer):
        """Season from the offer's products (they carry it). Majority wins."""
        seasons = []
        try:
            for op in offer.products.all():
                s = getattr(op.product, "season", "") or ""
                if s:
                    seasons.append(s)
        except Exception:
            pass
        if not seasons:
            # fall back to a hint in the name
            n = (offer.name or "").lower()
            if "hiver" in n or "winter" in n:
                return "winter"
            if "summer" in n or "ete" in n or "été" in n:
                return "summer"
            return ""
        # majority
        return max(set(seasons), key=seasons.count)

    def handle(self, *args, **opts):
        apply_changes = opts.get("apply", False)
        offers = Offer.objects.all().order_by("name")
        cat_set = seas_set = blank_left = 0
        for o in offers:
            changed = []
            if not (o.category or "").strip():
                cat = _guess_category(o.name)
                if cat:
                    o.category = cat
                    changed.append("category")
                    cat_set += 1
            if not (o.season or "").strip():
                seas = self._guess_season(o)
                if seas:
                    o.season = seas
                    changed.append("season")
                    seas_set += 1
            if changed:
                self.stdout.write(
                    f"  {'SET' if apply_changes else 'a definir'}: {o.name} -> "
                    f"{o.season or '?'} / {o.category or '?'}")
                if apply_changes:
                    o.save(update_fields=changed + ["updated_at"])
            elif not (o.category or "").strip():
                blank_left += 1
                self.stdout.write(f"  ? {o.name} -- categorie non devinee (a faire a la main)")

        self.stdout.write("")
        msg = (f"{cat_set} categorie(s) + {seas_set} saison(s) definies, "
               f"{blank_left} categorie(s) a faire a la main.")
        if apply_changes:
            self.stdout.write(self.style.SUCCESS("Termine. " + msg))
        else:
            self.stdout.write(self.style.WARNING("Simulation: " + msg + " Relancez avec --apply."))
