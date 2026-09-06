"""Subscribe our pages to this app WITH the message_echoes field, so Meta
delivers the Page's OUTGOING replies (e.g. Unifunl's answers) to our webhook —
giving us the full conversation, not just the customer's side.

Dry-run lists each page's current subscribed_fields and whether echoes are on.
--apply subscribes every configured page with the messaging fields incl.
message_echoes.

Note: the `message_echoes` webhook field must ALSO be enabled in the Meta App
dashboard (App → Webhooks → Messenger/Instagram) for delivery to actually
happen. This command handles the per-page subscription; the dashboard toggle is
manual.

Usage:
    python manage.py enable_message_echoes            # dry-run (show status)
    python manage.py enable_message_echoes --apply    # subscribe with echoes
"""
import json
import os
import urllib.parse
import urllib.request

from django.core.management.base import BaseCommand

FIELDS = ("messages,message_echoes,messaging_postbacks,messaging_optins,"
          "message_deliveries,message_reads,messaging_referrals")
GRAPH = "https://graph.facebook.com/v21.0"


class Command(BaseCommand):
    help = "Subscribe pages with message_echoes so we receive outgoing replies."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Actually subscribe. Without it, only shows status.")

    def _pairs(self):
        raw = os.environ.get("MESSENGER_PAGE_TOKENS", "")
        out = []
        for pair in raw.split(","):
            pair = pair.strip()
            if pair and ":" in pair:
                pid, _, tok = pair.partition(":")
                out.append((pid.strip(), tok.strip()))
        return out

    def handle(self, *args, **opts):
        pairs = self._pairs()
        if not pairs:
            self.stdout.write(self.style.ERROR(
                "MESSENGER_PAGE_TOKENS n'est pas configuré — aucune page à souscrire."))
            return
        apply_changes = opts.get("apply", False)

        for pid, tok in pairs:
            # Current subscription status.
            try:
                url = f"{GRAPH}/{pid}/subscribed_apps?access_token={urllib.parse.quote(tok, safe='')}"
                with urllib.request.urlopen(url, timeout=20) as r:
                    cur = json.load(r)
                fields = []
                for app in cur.get("data", []):
                    fields += app.get("subscribed_fields", []) or []
                fields = sorted(set(fields))
                has_echo = "message_echoes" in fields
                self.stdout.write(
                    f"Page {pid}: echoes={'OUI' if has_echo else 'NON'} | champs={fields}")
            except Exception as e:
                body = ""
                try:
                    if hasattr(e, "read"):
                        body = e.read().decode("utf-8", "replace")[:200]
                except Exception:
                    pass
                self.stdout.write(f"Page {pid}: erreur lecture — {str(e)[:120]} {body}")
                continue

            if not apply_changes:
                continue

            # Subscribe (idempotent) with the messaging fields incl. echoes.
            try:
                data = urllib.parse.urlencode(
                    {"subscribed_fields": FIELDS, "access_token": tok}).encode("utf-8")
                req = urllib.request.Request(
                    f"{GRAPH}/{pid}/subscribed_apps", data=data, method="POST")
                with urllib.request.urlopen(req, timeout=20) as r:
                    res = json.load(r)
                self.stdout.write(self.style.SUCCESS(
                    f"  → souscrit avec message_echoes : {res}"))
            except Exception as e:
                body = ""
                try:
                    if hasattr(e, "read"):
                        body = e.read().decode("utf-8", "replace")[:250]
                except Exception:
                    pass
                self.stdout.write(self.style.ERROR(
                    f"  → ERREUR souscription — {str(e)[:120]} {body}"))

        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                "\nDry-run. Relance avec --apply pour souscrire les pages avec message_echoes."))
