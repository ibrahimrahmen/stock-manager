from django.apps import AppConfig


class InventoryConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "inventory"

    def ready(self):
        # Import signal handlers so they get registered.
        # Imported here (inside ready) to avoid circular imports at module load.
        from . import signals  # noqa: F401

        # Start the background Messenger poller (in-app, no external scheduler).
        # Controlled by env var MESSENGER_POLL_INTERVAL (seconds); 0/unset = off.
        self._maybe_start_messenger_poller()

    def _maybe_start_messenger_poller(self):
        import os
        import sys
        import threading
        import time

        # Only run in the actual server process, not in migrations/shell/etc.
        # and not in the autoreloader's first process during local dev.
        argv = " ".join(sys.argv)
        if any(cmd in argv for cmd in ("migrate", "makemigrations", "collectstatic",
                                       "shell", "createsuperuser", "poll_messenger",
                                       "test")):
            return
        try:
            interval = int(os.environ.get("MESSENGER_POLL_INTERVAL", "0"))
        except ValueError:
            interval = 0
        if interval <= 0:
            return
        # Guard against double-start (e.g. multiple workers): allow, but each
        # worker polling is harmless because message dedup prevents duplicates.

        def _loop():
            # Small initial delay so the app finishes booting first.
            time.sleep(30)
            while True:
                try:
                    from .views import (MESSENGER_PAGE_TO_SALESPAGE,
                                        _messenger_poll_page)
                    for page_id in MESSENGER_PAGE_TO_SALESPAGE.keys():
                        try:
                            _messenger_poll_page(page_id)
                        except Exception:
                            pass
                except Exception:
                    pass
                time.sleep(max(interval, 60))

        t = threading.Thread(target=_loop, daemon=True, name="messenger-poller")
        t.start()
