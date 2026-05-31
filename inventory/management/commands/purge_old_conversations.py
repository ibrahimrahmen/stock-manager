"""Purge old Messenger conversation text from orders.

Deletes `conversation_text` on orders whose conversation is older than N days
(default 10). This keeps the database lean and is privacy-friendly.

What is KEPT:
    - The order itself, and all its fields.
    - The customer's `customer_psid` (lives on Customer, never purged here).
      This is the linking key, so a returning customer can have a fresh
      conversation re-attached later.

What is CLEARED:
    - Only the bulky `conversation_text` field, and `conversation_updated_at`.

Usage:
    python manage.py purge_old_conversations              # dry-run (lists only)
    python manage.py purge_old_conversations --apply      # actually clear
    python manage.py purge_old_conversations --days 14 --apply
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from inventory.models import Order


class Command(BaseCommand):
    help = "Clear Messenger conversation_text older than N days (default 10)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days", type=int, default=10,
            help="Age in days after which conversation text is purged (default 10).",
        )
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually clear the text. Without this flag it is a dry-run.",
        )

    def handle(self, *args, **options):
        days = options["days"]
        apply = options["apply"]
        cutoff = timezone.now() - timedelta(days=days)

        qs = (Order.objects
              .exclude(conversation_text="")
              .filter(conversation_updated_at__lt=cutoff))

        count = qs.count()
        if count == 0:
            self.stdout.write(f"No conversations older than {days} days. Nothing to do.")
            return

        self.stdout.write(
            f"{'CLEARING' if apply else 'WOULD CLEAR'} conversation text on "
            f"{count} order(s) older than {days} days (cutoff {cutoff:%Y-%m-%d %H:%M})."
        )
        for o in qs.values("id", "conversation_updated_at")[:50]:
            self.stdout.write(f"  - Order #{o['id']} (updated {o['conversation_updated_at']})")
        if count > 50:
            self.stdout.write(f"  … and {count - 50} more.")

        if apply:
            updated = qs.update(conversation_text="", conversation_updated_at=None)
            self.stdout.write(self.style.SUCCESS(f"Cleared conversation text on {updated} order(s)."))
        else:
            self.stdout.write("Dry-run only. Re-run with --apply to actually clear.")
