"""
Poll Messenger conversations from all configured pages and create pending
orders. Runs server-side (no login needed) so it can be scheduled.

Usage:
    python manage.py poll_messenger

Schedule it (e.g. every 3 minutes) via Railway cron or any scheduler.
"""
from django.core.management.base import BaseCommand

from inventory.views import (
    MESSENGER_PAGE_TO_SALESPAGE,
    _messenger_poll_page,
)


class Command(BaseCommand):
    help = "Poll Messenger conversations from configured pages and create orders."

    def handle(self, *args, **options):
        total_msgs = 0
        for page_id in MESSENGER_PAGE_TO_SALESPAGE.keys():
            try:
                seen, added = _messenger_poll_page(page_id)
            except Exception as e:
                self.stderr.write(f"page {page_id}: error {e}")
                continue
            if seen or added:
                self.stdout.write(
                    f"page {page_id}: {seen} conversations, {added} new messages"
                )
            total_msgs += added
        self.stdout.write(self.style.SUCCESS(
            f"Done. {total_msgs} new message(s) processed."
        ))
