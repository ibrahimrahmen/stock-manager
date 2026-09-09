"""Automatic Google Drive sync of the Barats catalogue.

Uploads a catalogue document (product photos + descriptions + prices + colours)
into a Google Drive folder you share with a service account, so Meta's AI (or
anything reading that folder) always has your current offers. Server-to-server
via a service account — no weekly OAuth token expiry.

Config (set in the in-app Configuration Center):
  GOOGLE_SA_JSON         - the service account key JSON (secret, encrypted)
  GOOGLE_DRIVE_FOLDER_ID - the target Drive folder id (share it with the SA)
  SITE_URL               - public base URL for absolute image links
                           (default: the Railway domain)

The catalogue is uploaded as a Google Doc (HTML converted on import) named
"Catalogue Barats", updated in place on each sync.
"""
import html as _html
import json as _json


_DEFAULT_SITE = "https://web-production-1391c5.up.railway.app"
_DOC_NAME = "Catalogue Barats"


class _UrlShim:
    """Minimal stand-in for a request, so unifunl_commerce helpers can build
    absolute image URLs without an HTTP request."""
    def __init__(self, base):
        self.base = (base or _DEFAULT_SITE).rstrip("/")

    def build_absolute_uri(self, u):
        if not u:
            return u
        if u.startswith("http"):
            return u
        return self.base + (u if u.startswith("/") else "/" + u)


def _site_base():
    from .models import get_setting
    return get_setting("SITE_URL", "") or _DEFAULT_SITE


def build_catalog_items():
    """[{name, price, description, images[], colors[{label,image}]}] for the
    Barats/Barats.tn offers, with ABSOLUTE image URLs."""
    from . import unifunl_commerce as uc
    shim = _UrlShim(_site_base())
    items = []
    for o in uc._barats_offers_qs().order_by("name"):
        try:
            prod = uc._offer_to_product(o, shim)
        except Exception:
            continue
        try:
            price = int(round(float(o.price_for_page_name("Barats")
                                    or o.bundle_price or 0)))
        except Exception:
            price = o.bundle_price or 0
        colors = []
        for v in prod.get("variants", []):
            lbl = (v.get("options") or {}).get("Couleur", "")
            if v.get("image") or lbl:
                colors.append({"label": lbl, "image": v.get("image")})
        items.append({
            "name": prod.get("title") or o.name,
            "price": price,
            "description": prod.get("description") or "",
            "images": prod.get("images") or [],
            "colors": colors,
        })
    return items


def build_catalog_html():
    """Clean, semantic HTML for Drive→Google Doc conversion (text + images)."""
    items = build_catalog_items()
    parts = ["<html><head><meta charset='utf-8'></head><body>",
             "<h1>Catalogue Barats</h1>"]
    for it in items:
        title = _html.escape(it["name"])
        price = it["price"]
        parts.append("<h2>%s%s</h2>" % (
            title, (" — %s DT" % price) if price else ""))
        if it["images"]:
            parts.append("<p><img src='%s' width='420'></p>"
                         % _html.escape(it["images"][0]))
        if it["description"]:
            parts.append("<p>%s</p>" % _html.escape(it["description"]).replace("\n", "<br>"))
        if it["colors"]:
            labels = [_html.escape(c["label"]) for c in it["colors"] if c.get("label")]
            if labels:
                parts.append("<p><b>Couleurs :</b> %s</p>" % ", ".join(labels))
            for c in it["colors"]:
                if c.get("image"):
                    cap = _html.escape(c.get("label") or "")
                    parts.append("<p><img src='%s' width='200'><br>%s</p>"
                                 % (_html.escape(c["image"]), cap))
        parts.append("<hr>")
    parts.append("</body></html>")
    return "".join(parts), len(items)


def _drive_service():
    """Build an authenticated Drive service from the service-account JSON, or
    return (None, error)."""
    from .models import get_setting
    raw = get_setting("GOOGLE_SA_JSON", "").strip()
    if not raw:
        return None, "GOOGLE_SA_JSON manquant (clé du compte de service)."
    try:
        info = _json.loads(raw)
    except Exception as e:
        return None, "GOOGLE_SA_JSON invalide (JSON): %s" % str(e)[:120]
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/drive"])
        svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        return svc, ""
    except Exception as e:
        return None, "Auth Google échouée: %s" % str(e)[:160]


def sync_catalog_to_drive():
    """Generate the catalogue and upload/update it as a Google Doc in the target
    Drive folder. Returns a dict summary (never raises)."""
    from .models import get_setting
    folder = get_setting("GOOGLE_DRIVE_FOLDER_ID", "").strip()
    if not folder:
        return {"ok": False, "error": "GOOGLE_DRIVE_FOLDER_ID manquant."}
    svc, err = _drive_service()
    if svc is None:
        return {"ok": False, "error": err}
    try:
        from googleapiclient.http import MediaInMemoryUpload
        html_str, n = build_catalog_html()
        media = MediaInMemoryUpload(html_str.encode("utf-8"),
                                    mimetype="text/html", resumable=False)
        # Find the existing doc by name in the folder.
        q = ("name = '%s' and '%s' in parents and trashed = false"
             % (_DOC_NAME, folder))
        found = svc.files().list(q=q, fields="files(id)",
                                 pageSize=1).execute().get("files", [])
        if found:
            fid = found[0]["id"]
            svc.files().update(fileId=fid, media_body=media).execute()
            action = "updated"
        else:
            meta = {"name": _DOC_NAME, "parents": [folder],
                    "mimeType": "application/vnd.google-apps.document"}
            created = svc.files().create(body=meta, media_body=media,
                                         fields="id").execute()
            fid = created.get("id")
            action = "created"
        return {"ok": True, "action": action, "file_id": fid, "products": n}
    except Exception as e:
        body = ""
        try:
            if hasattr(e, "content"):
                body = e.content.decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return {"ok": False, "error": ("%s %s" % (str(e)[:200], body)).strip()}
