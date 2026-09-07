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

# Facebook Pages: message_echoes is a first-class webhook field.
FIELDS_FB = ("messages,message_echoes,messaging_postbacks,messaging_optins,"
             "message_deliveries,message_reads,messaging_referrals")
# Instagram (Instagram Login) uses a different host AND a different field set —
# there is no separate 'message_echoes' field; echoes of the business's own
# messages arrive via 'messages'. Instagram tokens are INVALID on
# graph.facebook.com ("Cannot parse access token"), which is why a FB-only check
# looked like the IG tokens were broken when they are fine.
FIELDS_IG = ("messages,messaging_postbacks,messaging_seen,message_reactions,"
             "messaging_referral")
GRAPH_FB = "https://graph.facebook.com/v21.0"
GRAPH_IG = "https://graph.instagram.com/v21.0"
GRAPH = GRAPH_FB  # kept for backward reference


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

    def _read_status(self, host, pid, tok):
        """Read subscribed_apps from a given host. Returns (fields, err_body).
        fields is a sorted list on success; None on failure."""
        url = (f"{host}/{pid}/subscribed_apps"
               f"?access_token={urllib.parse.quote(tok, safe='')}")
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                cur = json.load(r)
            fields = []
            for app in cur.get("data", []):
                fields += app.get("subscribed_fields", []) or []
            return sorted(set(fields)), ""
        except Exception as e:
            body = ""
            try:
                if hasattr(e, "read"):
                    body = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            return None, f"{str(e)[:100]} {body}"

    def handle(self, *args, **opts):
        pairs = self._pairs()
        if not pairs:
            self.stdout.write(self.style.ERROR(
                "MESSENGER_PAGE_TOKENS n'est pas configuré — aucune page à souscrire."))
            return
        apply_changes = opts.get("apply", False)

        for pid, tok in pairs:
            # An account can be a Facebook Page OR an Instagram (IG Login) id.
            # FB tokens work on graph.facebook.com; IG tokens only on
            # graph.instagram.com. Try FB first, then IG, so IG accounts are
            # reported correctly instead of "Cannot parse access token".
            fields, fb_err = self._read_status(GRAPH_FB, pid, tok)
            host, kind, want_fields = GRAPH_FB, "FB", FIELDS_FB
            if fields is None:
                ig_fields, ig_err = self._read_status(GRAPH_IG, pid, tok)
                if ig_fields is not None:
                    fields, host, kind, want_fields = (
                        ig_fields, GRAPH_IG, "IG", FIELDS_IG)
                else:
                    self.stdout.write(
                        f"{pid}: erreur lecture — FB[{fb_err}] IG[{ig_err}]")
                    continue

            if kind == "FB":
                has_echo = "message_echoes" in fields
                self.stdout.write(
                    f"Page FB {pid}: echoes={'OUI' if has_echo else 'NON'} "
                    f"| champs={fields}")
            else:
                # IG has no separate echo field; 'messages' carries echoes.
                has_msgs = "messages" in fields
                self.stdout.write(
                    f"Compte IG {pid}: messages={'OUI' if has_msgs else 'NON'} "
                    f"(les échos IG passent par 'messages') | champs={fields}")

            if not apply_changes:
                continue

            # Subscribe (idempotent) with the right field set for this host.
            try:
                data = urllib.parse.urlencode(
                    {"subscribed_fields": want_fields, "access_token": tok}
                ).encode("utf-8")
                req = urllib.request.Request(
                    f"{host}/{pid}/subscribed_apps", data=data, method="POST")
                with urllib.request.urlopen(req, timeout=20) as r:
                    res = json.load(r)
                self.stdout.write(self.style.SUCCESS(
                    f"  → souscrit ({kind}) : {res}"))
            except Exception as e:
                body = ""
                try:
                    if hasattr(e, "read"):
                        body = e.read().decode("utf-8", "replace")[:250]
                except Exception:
                    pass
                self.stdout.write(self.style.ERROR(
                    f"  → ERREUR souscription ({kind}) — {str(e)[:120]} {body}"))

        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                "\nDry-run. Relance avec --apply pour souscrire les pages/comptes."))
