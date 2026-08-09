"""Backfill the is_angry flag on existing orders.

Scans every order that has a captured DM conversation and sets is_angry based
on the keyword list in inventory/angry_words.py. Safe to re-run any time
(the word list can change; re-running re-evaluates all orders).

Usage:
    python manage.py flag_angry_orders            # dry-run (lists matches)
    python manage.py flag_angry_orders --apply    # actually update
"""
from django.core.management.base import BaseCommand

from inventory.models import Order
from inventory.angry_words import detect_angry


class Command(BaseCommand):
    help = "Flag orders whose DM conversation contains angry words (is_angry)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually apply the changes. Without this flag, only lists matches.",
        )

    def handle(self, *args, **opts):
        apply_changes = opts.get("apply", False)
        qs = Order.objects.exclude(conversation_text="").only(
            "id", "conversation_text", "is_angry"
        )
        to_true, to_false = [], []
        for o in qs.iterator():
            flag = detect_angry(o.conversation_text or "")
            if flag and not o.is_angry:
                to_true.append(o)
            elif not flag and o.is_angry:
                to_false.append(o)

        self.stdout.write(
            f"Orders to flag angry (now True): {len(to_true)} | "
            f"to clear (now False): {len(to_false)}"
        )
        for o in to_true[:50]:
            self.stdout.write(f"  angry  #{o.id}")

        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                "\nDry-run. Re-run with --apply to actually update."))
            return

        n = 0
        for o in to_true + to_false:
            o.is_angry = detect_angry(o.conversation_text or "")
            # Save is_angry directly; bypass the conversation-text hook path.
            super(Order, o).save(update_fields=["is_angry"])
            n += 1
        self.stdout.write(self.style.SUCCESS(f"\n{n} order(s) updated."))
