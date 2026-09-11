"""Repair the front/back product mismatch on orders.

Symptom: an order shows its products in the FRONT article list, but the offer
EDITOR shows the offer with no products (colour/size look empty), because the
product lines were never linked to their OrderOffer. Order #22271 was like this.

This finds every order that has an OrderOffer with zero linked lines that can be
rebuilt from the order's loose (standalone) lines, and relinks them. It never
creates, deletes, or merges genuine extra items, and never reduces an order's
value — it only sets the missing link. See Order.relink_loose_offer_lines for
the full safety rules.

Usage:
    python manage.py fix_order_offer_links            # dry-run (lists only)
    python manage.py fix_order_offer_links --apply    # actually relink
    python manage.py fix_order_offer_links --limit 20 # cap candidates scanned
"""
from django.core.management.base import BaseCommand
from django.db.models import Count
from inventory.models import Order, OrderOffer, AuditLog, log_action


class Command(BaseCommand):
    help = "Relink loose product lines to their OrderOffer (front/back consistency)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually relink. Without this flag, only lists candidates.",
        )
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Only process the first N candidate orders (0 = all).",
        )

    def handle(self, *args, **opts):
        apply_changes = opts.get("apply", False)
        limit = opts.get("limit") or 0

        # Candidate orders: at least one OrderOffer with zero lines AND at least
        # one standalone (order_offer NULL) line that could be claimed.
        order_ids = list(
            OrderOffer.objects.annotate(nl=Count("lines"))
            .filter(nl=0).values_list("order_id", flat=True).distinct()
        )
        qs = (Order.objects.filter(id__in=order_ids)
              .filter(lines__order_offer__isnull=True)
              .distinct().order_by("id"))

        total = qs.count()
        self.stdout.write(
            f"Scanning {total} candidate order(s) (empty OrderOffer + loose lines)…")

        seen = planned = applied = skipped_locked = 0
        for order in qs.iterator():
            res = order.relink_loose_offer_lines(apply=apply_changes)
            if not res.get("linked"):
                continue  # nothing safely claimable on this order
            seen += 1
            if res.get("skipped_locked"):
                skipped_locked += 1
                self.stdout.write(
                    f"  #{order.id}: SKIP (verrouillée, total changerait "
                    f"{res['old_total']}→{res['new_total']}) — {res['detail']}")
            else:
                planned += 1
                tag = "RELINKED" if res.get("applied") else "à relier"
                self.stdout.write(
                    f"  #{order.id}: {tag} — {res['detail']} "
                    f"(total {res['old_total']}→{res['new_total']})")
                if res.get("applied"):
                    applied += 1
                    log_action(
                        None, AuditLog.EDIT,
                        description=(
                            f"Consistance: commande #{order.id} — "
                            f"{len(res['linked'])} ligne(s) reliée(s) à leur offre "
                            f"(total {res['old_total']}→{res['new_total']})"),
                        target_model="Order", target_id=order.id,
                    )
            if limit and seen >= limit:
                self.stdout.write(f"  (arrêt à limit={limit})")
                break

        self.stdout.write("")
        if apply_changes:
            self.stdout.write(self.style.SUCCESS(
                f"Terminé. {applied} commande(s) reliée(s), "
                f"{skipped_locked} ignorée(s) (verrouillées)."))
        else:
            self.stdout.write(self.style.WARNING(
                f"Simulation : {planned} commande(s) seraient reliées, "
                f"{skipped_locked} ignorée(s) (verrouillées). "
                f"Relancez avec --apply pour appliquer."))
