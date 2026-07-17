import json
import os
from decimal import Decimal
from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse, HttpResponseForbidden, HttpResponse
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.db import transaction
from django.db.models import Count, Q
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required

from .models import (
    Product, ProductVariant, ProductUnit,
    ShippingOrder, OrderItem, StockMovement, Payment, SizeAlert, OrderVerification, ScanSessionLog,
)
from .scan_service import handle_shipping_scan, handle_stock_scan


def _user_for_request(request):
    """Return the authenticated user or None."""
    user = getattr(request, 'user', None)
    if user is not None and getattr(user, 'is_authenticated', False):
        return user
    return None


def get_scan_session_date():
    """Return the date that scans should be grouped under.

    Normal days: today's date.
    Sunday (weekday=6): returns Saturday's date — the shipping company doesn't
    pick up packages on Saturday, only on Sunday. So Saturday + Sunday scans
    are fused into a single session under the Saturday date. The session
    only resets on Monday.
    """
    today = timezone.now().date()
    if today.weekday() == 6:  # Sunday
        # Roll back to Saturday (1 day earlier)
        from datetime import timedelta
        return today - timedelta(days=1)
    return today


# Navex API URL — token comes from env var, never hard-coded.
# Set NAVEX_API_TOKEN in Railway variables.
NAVEX_API_URL = (
    f"https://app.navex.tn/api/rashop-etat-"
    f"{os.environ.get('NAVEX_API_TOKEN', '')}/v1/post.php"
)

# Messenger Facebook Page ID → SalesPage id. A DM to each Page creates the
# order on the mapped sales_page. Unmapped Pages fall back to 3 (Barats).
# Barats.tn (1) is the SHOPIFY store, NOT a Messenger page — never the default.
MESSENGER_PAGE_TO_SALESPAGE = {
    "179384998586489": 2,    # Arrow Sportswear    → Arrow SportsWear
    "580021675198711": 3,    # Barats              → Barats
    "296257303579418": 4,    # Next Generation     → Next Generation
    "212577788599999": 5,    # Handsome collection → Handsome Collection
    "859568317250953": 6,    # Primefit.tn         → PrimeFit
    "494370360430121": 10,   # Traffic.tn          → Traffic
    # Instagram accounts (keyed by IG account id, same as FB pages)
    "17841474699259489": 3,  # @barats216 (IG)          → Barats
    "17841473682487619": 4,  # @next.generation216 (IG) → Next Generation
    "17841471034197351": 10, # @traffica.tn (IG)        → Traffic
    "17841474217754022": 5,  # @handsome.collection216  → Handsome Collection
    "17841479779102575": 6,  # @primefit_tn (IG)        → PrimeFit
    "17841466972227220": 2,  # @arrowsportswear.tn (IG) → Arrow SportsWear
}
MESSENGER_DEFAULT_SALESPAGE = 3   # Barats (fallback for unmapped pages)


def _extract_tn_phone(text):
    """Extract a valid Tunisian mobile (8 digits) from free text.

    Returns the 8-digit string or '' if none found. Guards against grabbing
    digits out of URLs / Facebook post IDs (e.g. 'replied to a post' lines)
    and only accepts realistic Tunisian mobile prefixes (2, 4, 5, 7, 9).
    """
    import re as _r
    if not text:
        return ""
    # Drop URLs and the FB "replied to a post" noise — their long numeric IDs
    # were being mis-read as phone numbers.
    cleaned = _r.sub(r"https?://\S+", " ", text)
    cleaned = _r.sub(r"(?i)replied to a post\.?", " ", cleaned)
    cleaned = _r.sub(r"(?i)a répondu à une publication\.?", " ", cleaned)
    cleaned = _r.sub(r"(?i)view post", " ", cleaned)
    # Find 8-digit groups that stand alone (allow spaces inside like "20 123 456"),
    # not embedded in a longer digit run.
    for raw in _r.findall(r"(?<!\d)(?:\+?216[\s-]?)?(\d[\d\s-]{6,}\d)(?!\d)", cleaned):
        digits = _r.sub(r"\D", "", raw)
        if len(digits) == 11 and digits.startswith("216"):
            digits = digits[3:]
        if len(digits) == 8 and digits[0] in "2457 9".replace(" ", ""):
            return digits
    return ""


def _messenger_page_token(page_id):
    """Page access token for sending replies. Tokens are stored in the env var
    MESSENGER_PAGE_TOKENS as 'page_id:token,page_id:token,...' so they're never
    hard-coded. Returns the token for the page, or '' if not configured."""
    raw = os.environ.get("MESSENGER_PAGE_TOKENS", "")
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        pid, _, tok = pair.partition(":")
        if pid.strip() == str(page_id):
            return tok.strip()
    return ""


# Auto-reply sent (once) when a customer messages — in Arabic, reassuring them
# the order was received and staff will follow up.
MESSENGER_AUTOREPLY_AR = (
    "أهلا بيك 🤍 وصلنا طلبك، فريقنا باش يتواصل معاك في أقرب وقت لتأكيد الكوموند. "
    "يرجى التثبت من رقم الهاتف، العنوان، المقاس واللون. شكرا لثقتك في Barats 🛍️"
)


# --- Auto-reply bot (Étape 1) -------------------------------------------------
# Toggle with env AUTOREPLY_BOT_ENABLED=1. Answers customer questions (price,
# availability, delivery) in Tunisian Arabic using Claude, BEFORE the customer
# sends a phone number. Once a phone arrives, the bot steps aside and the normal
# order flow / staff take over. Kept deliberately simple and safe.
BOT_SYSTEM_PROMPT_AR = (
    "Enti bayaa tounsi fi boutique melebs esmha Barats. T7ki maa el 7arif "
    "bel tounsi latin (arabizi) barka — kifeh ay tounsi aadi fel DM. "
    "Mbesset, direct, mrattah. Reply court: jomla wa la zouz barka.\n\n"

    "QAWAAID EL KLEM:\n"
    "- Tounsi latin barka (ma tektebch bel 3arbi). Tnajem tkhalet chwaya "
    "francais kima el tounsiyin (mathal 'disponible en...'). Ama "
    "esta3mel kelmet s7a7, ma tekhtere3ch kelmet.\n"
    "- Naadi el rajel 'khouya', el mra 'okhti'. 3omrek ma t9ol 'habibi'.\n"
    "- El thaman dima b 'X DT' wa la 'X dinar' (mathal 59 DT). 3omrek "
    "'alf' wala 'ألف'.\n"
    "- Ma tbe3thch el khatawet dakhliya (kima 'chouf el taswira') fel reply. "
    "Jaweb el 7arif direct.\n\n"

    "KIFEH TReponDI:\n"
    "1) Ki el 7arif ybda ('slm', 'aslema', mntej...): 'Aslema khouya, bech "
    "t3adi commande ab3athelna taille, adresse w noumrouk'.\n"
    "2) Ki yeb3ath taswira wa la ysemmi mntej: chouf el catalogue, al9a el "
    "mntej (el ecusson/logo kima FC Barcelone, Jordan, Nike howa a9wa dalil). "
    "Ba3d, jaweb b haka el forme EXACTE:\n"
    "   'Aslema khouya, hedha [ESM EL MNTEJ] b [THAMAN] DT w livraison 7 DT. "
    "Bech t3adi commande ab3athelna taille, adresse w noumrouk.'\n"
    "   Ma tal9ach el mntej fel catalogue? 9oll: '9ollek el equipe "
    "y2akdoulek el thaman.'\n"
    "3) Ki el 7arif ye3ti taille/adresse/noumrou, chkorou w 9oll el equipe "
    "bech tkammel el commande.\n\n"

    "MA3LOUMET: livraison 7 DT l kol tounes, khlas aand el istilem. "
    "Ma tekhtere3ch aswem wa la kelmet. Ma tab3athch liens."
)


def _describe_product_image(product):
    """Use Claude Vision on the product's variant images (up to 3 — one per
    colorway) to produce a detailed French visual description covering ALL the
    color variants, and save it to product.description. Returns the
    description or '' on failure."""
    try:
        vs = list(product.variants.filter(image__isnull=False)
                  .exclude(image="")[:3])
        paths = []
        labels = []
        for v in vs:
            try:
                paths.append(v.image.path)
                labels.append(getattr(v, "color_label", "") or "")
            except Exception:
                continue
        if not paths:
            return ""
        color_hint = ""
        if any(labels):
            color_hint = ("Couleurs référencées: "
                          + ", ".join(l for l in labels if l) + ". ")
        prompt = (
            "Voici " + ("les photos des variantes de couleur d'" if len(paths) > 1
                        else "la photo d'") + "un même vêtement. "
            + color_hint +
            "Écris une description visuelle DÉTAILLÉE en français (2-3 phrases "
            "max) pour identifier ce produit à partir d'une photo client. "
            "Obligatoire: type exact (ensemble/pull/pantalon/short/tenue...), "
            "TOUTES les couleurs des variantes montrées, logo/marque/écusson "
            "visible (ex: Nike, FC Barcelone), motifs précis (rayures et leur "
            "direction, camouflage, texture), et tout élément distinctif "
            "(bandes latérales, col, poches). "
            "RÉPONDS UNIQUEMENT avec la description brute: pas de titre, pas de "
            "markdown (aucun #), pas de préambule, pas de liste. Commence "
            "directement par le type de vêtement."
        )
        # Strip any markdown/preamble the model may still add.
        def _clean_desc(t):
            t = (t or "").strip()
            for junk in ("# Description du produit", "Description du produit",
                         "# Description", "Description:"):
                if t.lower().startswith(junk.lower()):
                    t = t[len(junk):]
            return t.strip().lstrip("#:").strip()
        desc = _claude_generate(prompt, max_tokens=250, temperature=0.2,
                                local_images=paths)
        desc = _clean_desc(desc)
        if desc:
            product.description = desc[:500]
            product.save(update_fields=["description"])
        return desc
    except Exception:
        return ""


def _fmt_price(price):
    """Format a price without trailing '.000' — 89.000 -> '89', 59.500 -> '59.5'."""
    try:
        from decimal import Decimal
        d = Decimal(str(price)).normalize()
        # normalize() can give scientific notation for integers; fix that
        s = format(d, "f")
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return s or "0"
    except Exception:
        return str(price)


def _build_catalog_for_conv(conv, limit=60):
    """Build a compact catalogue string of active offers (name + price +
    included pieces) for the sales page this conversation maps to, so the bot
    can look up a product's price. Best-effort; returns '' on failure."""
    try:
        from .models import Offer, SalesPage
        sp_id = MESSENGER_PAGE_TO_SALESPAGE.get(
            str(getattr(conv, "page_id", "") or ""), MESSENGER_DEFAULT_SALESPAGE)
        page = SalesPage.objects.filter(pk=sp_id).first()
        # ALL active offers (any page): a customer may DM one page about a
        # product sold on another. Price still resolves for THIS page when an
        # override exists, else the default bundle price.
        offers = list(Offer.objects.filter(is_active=True).distinct()[:limit])
        lines = []
        for o in offers:
            try:
                price = o.price_for_page(page) if page else o.bundle_price
            except Exception:
                price = o.bundle_price
            # Included pieces + their visual descriptions (so the bot can
            # match a customer photo to a bundle by appearance, not just name).
            pieces = []
            descs = []
            try:
                for op in o.products.all():
                    prod = getattr(op, "product", None)
                    if not prod:
                        continue
                    if prod.name:
                        pieces.append(prod.name)
                    d = (getattr(prod, "description", "") or "").strip()
                    if d:
                        descs.append(d)
            except Exception:
                pass
            piece_str = (" (" + ", ".join(pieces) + ")") if pieces else ""
            desc_str = (" [" + " ; ".join(descs) + "]") if descs else ""
            lines.append(f"- {o.name}{piece_str} : {_fmt_price(price)} DT{desc_str}")
        return "\n".join(lines)
    except Exception:
        return ""


def _offers_data_for_conv(conv, limit=60):
    """Return structured offer data [{'name','price','desc'}] for matching."""
    out = []
    try:
        from .models import Offer, SalesPage
        sp_id = MESSENGER_PAGE_TO_SALESPAGE.get(
            str(getattr(conv, "page_id", "") or ""), MESSENGER_DEFAULT_SALESPAGE)
        page = SalesPage.objects.filter(pk=sp_id).first()
        for o in Offer.objects.filter(is_active=True).distinct()[:limit]:
            try:
                price = o.price_for_page(page) if page else o.bundle_price
            except Exception:
                price = o.bundle_price
            descs = []
            for op in o.products.all():
                prod = getattr(op, "product", None)
                if prod:
                    d = (getattr(prod, "description", "") or "").strip()
                    if d:
                        descs.append(d)
            out.append({"name": o.name, "price": _fmt_price(price),
                        "desc": " ; ".join(descs)})
    except Exception:
        pass
    return out


def _match_product_by_image(local_images, url_images, offers_data):
    """Two-step visual match. Step 1: Claude describes the photo (type, colors,
    logo, pattern). Step 2: we preselect offers whose stored description shares
    keywords, then ask Claude to pick the best among that short list. Returns a
    dict {name, price} or None. Much more reliable than showing all 43 offers
    at once. offers_data = [{'name','price','desc'}]."""
    try:
        # --- Step 1: describe what's in the customer's photo ---
        desc_prompt = (
            "Décris ce vêtement précisément en français (2 phrases max). "
            "OBLIGATOIRE: (1) type exact (ensemble polo+pantalon, pull, "
            "gilet sans manches, short...), (2) TOUTES les couleurs, "
            "(3) logo/marque visible (Nike, Zara, FC Barcelone, Jordan...), "
            "(4) le MOTIF EXACT et sa forme: distingue bien rayures "
            "HORIZONTALES vs VERTICALES, motif géométrique/jacquard "
            "(grecques, diamants, carrés), camouflage, uni. (5) type de "
            "col (polo, rond, montant) et manches (courtes/longues). "
            "Sois précis sur le motif car c'est ce qui distingue les "
            "produits similaires. Juste la description."
        )
        seen = _claude_generate(desc_prompt, max_tokens=120, temperature=0.1,
                                image_urls=url_images or None,
                                local_images=local_images or None)
        seen = (seen or "").strip()
        if not seen:
            return None

        # --- Preselect candidates by keyword overlap with stored descriptions ---
        import re as _re
        def _tokens(t):
            t = (t or "").lower()
            # keep meaningful words (colors, brands, types), drop tiny/common
            words = _re.findall(r"[a-zàâçéèêëîïôûùüÿ]+", t)
            stop = {"de", "un", "une", "des", "le", "la", "les", "avec", "et",
                    "en", "sur", "du", "au", "aux", "pour", "ce", "cette", "un",
                    "à", "d", "l", "the", "a", "of", "with"}
            return set(w for w in words if len(w) >= 3 and w not in stop)
        seen_tok = _tokens(seen)
        scored = []
        for od in offers_data:
            ot = _tokens(od.get("desc", "")) | _tokens(od.get("name", ""))
            overlap = len(seen_tok & ot)
            if overlap:
                scored.append((overlap, od))
        scored.sort(key=lambda x: -x[0])
        candidates = [od for _, od in scored[:10]]
        if not candidates:
            # nothing shares keywords — let the bot escalate to the team
            return {"_seen": seen, "_no_candidate": True}

        # --- Step 2: pick the best among the short candidate list ---
        clist = "\n".join(
            f"{i+1}. {c['name']} : {c['price']} DT — {c.get('desc','')[:180]}"
            for i, c in enumerate(candidates))
        pick_prompt = (
            "Un client a envoyé une photo d'un vêtement. Voici ce qu'on y voit:\n"
            + seen + "\n\nVoici les produits candidats du catalogue:\n" + clist
            + "\n\nQuel numéro correspond le mieux ? Compare surtout: le "
            "TYPE (ensemble/pull/gilet), le MOTIF EXACT (rayures "
            "horizontales vs verticales, géométrique/grecques/diamants, "
            "camouflage), et les couleurs. Le motif est le critère "
            "décisif entre produits similaires. Réponds UNIQUEMENT par le "
            "numéro (ex: 3). Si aucun ne correspond vraiment, réponds 0."
        )
        pick_prompt2 = (pick_prompt
            + "\n\nFormat: le numéro, une virgule, puis 'sur' si tu es "
            "certain (motif+type+couleurs identiques) ou 'pasur' si plusieurs "
            "candidats se ressemblent et tu hésites. Ex: '3,sur' ou '5,pasur'.")
        pick = _claude_generate(pick_prompt2, max_tokens=12, temperature=0.0)
        pick = (pick or "").strip().lower()
        m = _re.search(r"\d+", pick)
        if not m:
            return {"_seen": seen, "_no_candidate": True}
        idx = int(m.group())
        if idx == 0 or idx > len(candidates):
            return {"_seen": seen, "_no_candidate": True}
        chosen = candidates[idx - 1]
        confident = ("pasur" not in pick and "pas sur" not in pick)
        return {"name": chosen["name"], "price": chosen["price"],
                "confident": confident, "_seen": seen}
    except Exception:
        return None


def _bot_reply(conv):
    """Generate a short Tunisian-Arabic bot reply to the latest customer
    message, using the conversation so far. Returns the reply text or None.
    Best-effort; never raises. Simple Étape-1 version: Q&A only."""
    try:
        msgs = conv.messages or []
        # Build a compact transcript (last ~12 messages) for context. A message
        # may carry only an image (no text) — represent that explicitly so the
        # bot knows the customer sent a photo instead of silently ignoring it.
        lines = []
        last_is_photo_only = False
        for m in msgs[-12:]:
            who = "Client" if m.get("from") == "user" else "Vendeur"
            t = (m.get("text") or "").strip()
            has_img = bool(m.get("images"))
            if t and has_img:
                lines.append(f"{who}: {t}  [b3ath taswira]")
                last_is_photo_only = (m.get("from") == "user")
            elif t:
                lines.append(f"{who}: {t}")
                last_is_photo_only = False
            elif has_img:
                lines.append(f"{who}: [b3ath taswira mte3 mntej, bla text]")
                last_is_photo_only = (m.get("from") == "user")
        if not lines:
            return None
        transcript = "\n".join(lines)

        # Collect image URLs from the LAST customer message that carried photos,
        # so Claude Vision can actually see the product. We only send the most
        # recent batch (not the whole history) to avoid re-billing old images
        # on every turn.
        img_urls = []
        local_imgs = []
        try:
            # Test-page path: a local file injected on the conversation object.
            _lp = getattr(conv, "_test_local_image", "")
            if _lp:
                local_imgs = [_lp]
            else:
                for m in reversed(msgs):
                    if m.get("from") == "user" and m.get("images"):
                        img_urls = [u for u in (m.get("images") or [])
                                    if u and u != "local"][:3]
                        break
        except Exception:
            img_urls = []
            local_imgs = []

        # Guess gender from the customer's name so the bot uses خويا / أختي
        # correctly. Best-effort; falls back to neutral (خويا) when unknown.
        gender_hint = ""
        try:
            nm = (getattr(conv, "sender_name", "") or "").strip()
            if nm:
                g = _guess_gender_tn(nm)
                if g == "f":
                    gender_hint = "\n\n(El 7arifa '" + nm + "' mra — naadiha 'okhti'.)"
                elif g == "m":
                    gender_hint = "\n\n(El 7arif '" + nm + "' rajel — naadih 'khouya'.)"
        except Exception:
            gender_hint = ""

        # If the conversation came from a specific ad, fetch that ad's text —
        # it usually contains the product name and price, so the bot can answer
        # price questions accurately instead of asking for a photo.
        ad_context = ""
        try:
            _ad_id = (getattr(conv, "source_ad_id", "") or "").strip()
            if _ad_id:
                _ad_txt = _fetch_ad_text(_ad_id)
                if _ad_txt:
                    ad_context = (
                        "\n\nEl 7arif jé mel pub hedhi. Hedha nass el pub "
                        "(fih esm el mntej w el thaman — esta3mlou bech tjaweb "
                        "aala el thaman b da9a, ama ma t9rahch kifeh mektoub, "
                        "lkhesslou el ma3na):\n\"\"\"\n" + _ad_txt + "\n\"\"\""
                    )
        except Exception:
            ad_context = ""

        # Deterministic hint: has the bot already greeted in this conversation?
        # If so, tell it explicitly NOT to say "Aslema" again.
        greet_hint = ""
        try:
            already_greeted = any(
                m.get("from") == "page"
                and ("aslema" in (m.get("text") or "").lower()
                     or "aslama" in (m.get("text") or "").lower())
                for m in (conv.messages or [])
            )
            if already_greeted:
                greet_hint = ("\n\n(MOHIM: deja sallamt 'Aslema' fel "
                              "conversation — ma t3awedhech. Jaweb direct bla "
                              "salutation.)")
        except Exception:
            greet_hint = ""

        # Catalogue of this page's offers with prices, so the bot can look up a
        # product (named or seen in a photo) and quote the real price.
        catalog_context = ""
        try:
            _cat = _build_catalog_for_conv(conv)
            if _cat:
                catalog_context = (
                    "\n\nHedha el catalogue mte3 el produits (esm + thaman). "
                    "Esta3mlou bech tal9a el mntej (eli el 7arif semmeh wa la "
                    "eli chefto fel taswira) w a3ti el thaman mel catalogue. Ki "
                    "el mntej mech fel catalogue, 9ollou el equipe bech "
                    "t2akedlou:\n" + _cat
                )
        except Exception:
            catalog_context = ""

        # If the customer sent a photo, run the robust TWO-STEP visual match
        # (describe -> preselect candidates -> pick) instead of dumping all 43
        # offers on the vision call. Inject the result as a strong hint.
        match_hint = ""
        _matched = False
        try:
            if img_urls or local_imgs:
                _od = _offers_data_for_conv(conv)
                _res = _match_product_by_image(local_imgs, img_urls, _od)
                if _res and _res.get("name"):
                    if _res.get("confident", True):
                        match_hint = (
                            "\n\n(EL MNTEJ ELI FEL TASWIRA t3aref b da9a: '"
                            + _res["name"] + "' b " + str(_res["price"]) + " DT. "
                            "A3ti hedha el esm w hedha el thaman direct, w kammel "
                            "b jomlet el commande.)")
                    else:
                        match_hint = (
                            "\n\n(EL MNTEJ ELI FEL TASWIRA ychbeh l '"
                            + _res["name"] + "' b " + str(_res["price"]) + " DT ama "
                            "fama mntejat tochbehlou. 9oll lel 7arif eli ychbeh "
                            "l hedha el mntej b hedha el thaman, w el equipe "
                            "bech t2akedlou el modele bel dabt. Kammel b jomlet "
                            "el commande.)")
                    _matched = True
                elif _res and _res.get("_no_candidate"):
                    match_hint = (
                        "\n\n(El mntej eli fel taswira ma tlamch m3a 7atta "
                        "mntej fel catalogue b da9a. 9oll lel 7arif eli el "
                        "equipe bech t2akedlou el thaman w el disponibilite, "
                        "bla ma tekhtere3 esm wala thaman.)")
                    _matched = True
        except Exception:
            match_hint = ""
            _matched = False

        prompt = (
            BOT_SYSTEM_PROMPT_AR
            + gender_hint
            + greet_hint
            + ad_context
            + catalog_context
            + match_hint
            + "\n\nEl conversation lel7d ltew:\n" + transcript
            + "\n\nOkteb reply el bayaa ejjay barka bel tounsi latin (bla 'Vendeur:'): "
        )
        _fin_urls = [] if _matched else img_urls
        _fin_local = [] if _matched else local_imgs
        reply = _claude_generate(prompt, max_tokens=200, temperature=0.6,
                                 image_urls=_fin_urls, local_images=_fin_local)
        if not reply:
            return None
        reply = reply.strip().strip('"').strip()
        # Guard against the model echoing the label or going long.
        reply = reply.replace("Vendeur:", "").replace("البائع:", "").strip()
        return reply[:600] or None
    except Exception:
        return None


_TN_FEMALE_NAMES = {
    "arij", "ameny", "amani", "asma", "aya", "cyrine", "syrine", "dorra",
    "emna", "eya", "farah", "fatma", "feriel", "ghofrane", "ghofran", "hiba",
    "ines", "khaoula", "mariem", "maryem", "molka", "nour", "nesrine",
    "nesreen", "ons", "rania", "rihab", "rim", "salma", "sarra", "sirine",
    "syrine", "wafa", "wiem", "yasmine", "yosra", "zeineb", "zaineb", "hela",
    "amira", "chaima", "chaimaa", "ikram", "islem", "jihen", "jihene",
    "manel", "marwa", "mayssa", "meriem", "nada", "nadia", "olfa", "safa",
    "sana", "sonia", "takwa", "takoua", "wided", "yara", "hajer", "hajar",
}
_TN_MALE_NAMES = {
    "ahmed", "ali", "amine", "anis", "aymen", "bilel", "bilal", "chaker",
    "fares", "firas", "hamza", "hedi", "iheb", "ismail", "khalil", "mahdi",
    "malek", "marwen", "mehdi", "mohamed", "montassar", "nassim", "oussama",
    "rami", "seif", "seifeddine", "skander", "sofien", "sofiene", "wassim",
    "yassine", "yassin", "youssef", "zied", "aziz", "bassem", "bessem",
    "chedly", "fedi", "fadi", "ghassen", "haythem", "houssem", "jaber",
    "karim", "karem", "louay", "maher", "moez", "nizar", "ramzi", "riadh",
    "sami", "slim", "taha", "walid", "wael", "achref", "achraf", "chouaib",
}


def _guess_gender_tn(full_name):
    """Return 'm', 'f', or '' from a Tunisian display name using the first
    token. Best-effort; unknown names return ''."""
    try:
        import unicodedata as _ud
        first = (full_name or "").strip().split()[0].lower()
        first = "".join(c for c in _ud.normalize("NFD", first)
                        if _ud.category(c) != "Mn")
        if first in _TN_FEMALE_NAMES:
            return "f"
        if first in _TN_MALE_NAMES:
            return "m"
        if len(first) >= 3 and first.endswith(("a",)):
            return "f"
        return ""
    except Exception:
        return ""


def _fetch_dm_sender_name(page_id, sender_id, platform="messenger"):
    """Fetch the display name of a Messenger/Instagram user who messaged a page.
    Meta blocks the direct /{user_id}?fields=first_name endpoint for privacy, so
    we read the name from the conversation's participants list instead — the same
    source that already works for polled conversations. Returns "" on failure."""
    import urllib.request as _ureq
    import json as _json
    token = _messenger_page_token(page_id)
    if not token or not sender_id:
        return ""
    host = ("graph.instagram.com" if platform == "instagram"
            else "graph.facebook.com")
    # Look up the conversation with THIS user and read participant names.
    plat_q = "instagram" if platform == "instagram" else "messenger"
    url = (f"https://{host}/v21.0/{page_id}/conversations"
           f"?platform={plat_q}&user_id={sender_id}"
           f"&fields=participants&access_token={_ureq.quote(token, safe='')}")
    try:
        with _ureq.urlopen(url, timeout=6) as resp:
            d = _json.loads(resp.read().decode("utf-8"))
        for thread in d.get("data", []):
            for part in (thread.get("participants", {}) or {}).get("data", []):
                if str(part.get("id")) != str(page_id):
                    nm = (part.get("name") or part.get("username") or "").strip()
                    if nm:
                        return nm
    except Exception:
        pass
    return ""


def _resolve_ad_campaign_name(ad_id):
    """Resolve a Meta ad_id (from a Click-to-Messenger referral) to its campaign
    name. Returns the campaign name or "" on failure. Best-effort, short timeout,
    never raises — so it can't block the webhook."""
    import urllib.request as _ureq
    import json as _json
    if not ad_id:
        return ""
    token = os.environ.get("META_ACCESS_TOKEN", "").strip()
    if not token:
        return ""
    url = (f"https://graph.facebook.com/v21.0/{ad_id}"
           f"?fields=campaign{{name}}&access_token={token}")
    try:
        with _ureq.urlopen(url, timeout=6) as resp:
            d = _json.loads(resp.read().decode("utf-8"))
        return (d.get("campaign", {}) or {}).get("name", "") or ""
    except Exception:
        return ""


def _fetch_ad_text(ad_id):
    """Fetch the ad's creative text (the post/ad body the customer saw). This
    usually contains the product name and price, so the bot can answer price
    questions accurately when the customer came from a Click-to-Messenger ad.
    Returns the text or "" on failure. Best-effort, short timeout, cached."""
    import urllib.request as _ureq
    import json as _json
    if not ad_id:
        return ""
    token = os.environ.get("META_ACCESS_TOKEN", "").strip()
    if not token:
        return ""
    # Simple in-process cache so we don't refetch the same ad every message.
    global _AD_TEXT_CACHE
    try:
        _AD_TEXT_CACHE
    except NameError:
        _AD_TEXT_CACHE = {}
    if ad_id in _AD_TEXT_CACHE:
        return _AD_TEXT_CACHE[ad_id]
    # The ad's creative holds the text. Pull common text-bearing fields.
    url = (f"https://graph.facebook.com/v21.0/{ad_id}"
           f"?fields=creative{{body,title,object_story_spec,"
           f"asset_feed_spec,effective_object_story_id}}"
           f"&access_token={token}")
    text = ""
    try:
        with _ureq.urlopen(url, timeout=6) as resp:
            d = _json.loads(resp.read().decode("utf-8"))
        cr = d.get("creative", {}) or {}
        parts = []
        if cr.get("body"):
            parts.append(cr["body"])
        if cr.get("title"):
            parts.append(cr["title"])
        # object_story_spec.link_data.message / description
        oss = cr.get("object_story_spec", {}) or {}
        ld = oss.get("link_data", {}) or {}
        for k in ("message", "description", "name", "caption"):
            if ld.get(k):
                parts.append(ld[k])
        vd = oss.get("video_data", {}) or {}
        for k in ("message", "title"):
            if vd.get(k):
                parts.append(vd[k])
        # asset_feed_spec.bodies[].text / titles[].text (dynamic creatives)
        afs = cr.get("asset_feed_spec", {}) or {}
        for arr_key in ("bodies", "titles", "descriptions"):
            for item in (afs.get(arr_key, []) or []):
                if item.get("text"):
                    parts.append(item["text"])
        # Dedup preserving order, cap length.
        seen = set()
        uniq = []
        for pt in parts:
            pt = (pt or "").strip()
            if pt and pt not in seen:
                seen.add(pt)
                uniq.append(pt)
        text = "\n".join(uniq)[:1500]
    except Exception:
        text = ""
    _AD_TEXT_CACHE[ad_id] = text
    return text


def _messenger_send_text(page_id, recipient_id, text, platform="messenger"):
    """Send a text message back to a user via the Meta Send API. Best-effort:
    returns True on success, False otherwise (never raises).

    For Facebook Messenger, uses graph.facebook.com with the page token.
    For Instagram (Instagram Login), the identifier is the IG account id and
    the send endpoint lives on graph.instagram.com; the token for that IG id is
    stored in MESSENGER_PAGE_TOKENS keyed by the IG account id (same as pages).
    """
    import urllib.request as _ureq
    import json as _json
    token = _messenger_page_token(page_id)
    if not token or not recipient_id or not text:
        return False
    host = ("graph.instagram.com" if platform == "instagram"
            else "graph.facebook.com")
    url = (f"https://{host}/v21.0/me/messages?access_token="
           + _ureq.quote(token, safe=""))
    body = _json.dumps({
        "recipient": {"id": str(recipient_id)},
        "messaging_type": "RESPONSE",
        "message": {"text": text},
    }).encode("utf-8")
    try:
        req = _ureq.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        with _ureq.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False


def _claude_web_search(prompt, max_tokens=1024):
    """Call Claude WITH the web_search tool enabled. Returns the final text or
    None. Used to resolve which governorate a Tunisian locality belongs to when
    the name isn't obvious from our list. Slower/costlier than a plain call, so
    reserve it for the fallback path. Never raises; bails on 429."""
    import urllib.request as _ureq
    import json as _json
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key or not prompt:
        return None
    # Web search needs a capable model; Haiku may not support the tool well.
    model = os.environ.get("ANTHROPIC_SEARCH_MODEL", "claude-sonnet-4-5-20250929").strip()
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
    }
    data = _json.dumps(body).encode("utf-8")
    url = "https://api.anthropic.com/v1/messages"
    try:
        req = _ureq.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("x-api-key", api_key)
        req.add_header("anthropic-version", "2023-06-01")
        with _ureq.urlopen(req, timeout=40) as resp:
            rd = _json.loads(resp.read().decode("utf-8"))
        blocks = rd.get("content") or []
        txt = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
        return txt or None
    except Exception:
        return None


def _claude_generate(prompt, max_tokens=1024, temperature=0.0, cached_prefix=None, image_urls=None, local_images=None):
    """Call the Anthropic Claude API. Returns response text or None on failure.
    Replaces Gemini for DM order extraction and transliteration. Uses
    ANTHROPIC_API_KEY. On rate limit (429) it bails out immediately so a worker
    is never pinned. Model is configurable via ANTHROPIC_MODEL.

    If cached_prefix is given, it is sent as a separate content block marked
    with cache_control so Anthropic caches it (much cheaper on repeat). Use it
    for large unchanging context like the full delegation list."""
    import urllib.request as _ureq
    import json as _json
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key or not prompt:
        return None
    model = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001").strip()
    # Build the message content. If images are supplied (Claude Vision), send
    # them as image blocks BEFORE the text so the model sees them in context.
    # Meta CDN URLs (fbcdn.net) are blocked for Claude by robots.txt, so we
    # download each image ourselves and send it as base64 instead of a URL.
    _img_blocks = []
    for _u in (image_urls or [])[:3]:  # cap at 3 images to control cost
        if not _u:
            continue
        try:
            import base64 as _b64
            _ireq = _ureq.Request(_u, headers={"User-Agent": "Mozilla/5.0"})
            with _ureq.urlopen(_ireq, timeout=10) as _ir:
                _raw = _ir.read()
            # Detect the real format from magic bytes (Content-Type headers can
            # be wrong or generic); fall back to the header only if unknown.
            if _raw[:3] == b"\xff\xd8\xff":
                _mt = "image/jpeg"
            elif _raw[:8] == b"\x89PNG\r\n\x1a\n":
                _mt = "image/png"
            elif _raw[:6] in (b"GIF87a", b"GIF89a"):
                _mt = "image/gif"
            elif _raw[:4] == b"RIFF" and _raw[8:12] == b"WEBP":
                _mt = "image/webp"
            else:
                _mt = "image/jpeg"
            _b64data = _b64.b64encode(_raw).decode("ascii")
            _img_blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": _mt, "data": _b64data},
            })
        except Exception:
            # If a single image can't be fetched, skip it (bot still replies).
            continue
    for _lp in (local_images or [])[:3]:
        if not _lp:
            continue
        try:
            import base64 as _b64
            with open(_lp, "rb") as _lf:
                _raw = _lf.read()
            # Detect the REAL format from the file's magic bytes — many files
            # have a wrong extension (e.g. a .png that is actually JPEG), which
            # Claude rejects if the declared media_type doesn't match.
            _mt = "image/jpeg"
            if _raw[:3] == b"\xff\xd8\xff":
                _mt = "image/jpeg"
            elif _raw[:8] == b"\x89PNG\r\n\x1a\n":
                _mt = "image/png"
            elif _raw[:6] in (b"GIF87a", b"GIF89a"):
                _mt = "image/gif"
            elif _raw[:4] == b"RIFF" and _raw[8:12] == b"WEBP":
                _mt = "image/webp"
            else:
                _ext = (_lp.rsplit(".", 1)[-1] or "jpeg").lower()
                _mt = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                       "png": "image/png", "gif": "image/gif",
                       "webp": "image/webp"}.get(_ext, "image/jpeg")
            _img_blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": _mt,
                           "data": _b64.b64encode(_raw).decode("ascii")},
            })
        except Exception:
            continue
    if cached_prefix:
        msg_content = [
            {"type": "text", "text": cached_prefix,
             "cache_control": {"type": "ephemeral"}},
        ] + _img_blocks + [
            {"type": "text", "text": prompt},
        ]
    elif _img_blocks:
        msg_content = _img_blocks + [{"type": "text", "text": prompt}]
    else:
        msg_content = prompt
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": msg_content}],
    }
    data = _json.dumps(body).encode("utf-8")
    url = "https://api.anthropic.com/v1/messages"
    for retry in range(3):
        try:
            req = _ureq.Request(url, data=data, method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("x-api-key", api_key)
            req.add_header("anthropic-version", "2023-06-01")
            with _ureq.urlopen(req, timeout=15) as resp:
                rd = _json.loads(resp.read().decode("utf-8"))
            blocks = rd.get("content") or []
            txt = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
            return txt or None
        except Exception as e:
            es = str(e)
            # 429 = rate limit: don't retry (pins the worker); bail out.
            if "429" in es:
                return None
            # Retry only transient server errors, briefly.
            if ("529" in es or "503" in es or "500" in es
                    or "timed out" in es.lower() or "502" in es) and retry < 2:
                import time as _t; _t.sleep(1 + retry)
                continue
            return None
    return None


def _gemini_generate(prompt, max_tokens=1024, temperature=0.0, model="gemini-2.5-flash-lite"):
    """Backwards-compatible wrapper: now routes to Claude (Anthropic). Kept under
    the old name so existing callers work unchanged. The `model` arg is ignored
    (Claude model is chosen via ANTHROPIC_MODEL). Falls back to Gemini only if
    ANTHROPIC_API_KEY is absent but GEMINI_API_KEY is present."""
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return _claude_generate(prompt, max_tokens=max_tokens, temperature=temperature)
    return _gemini_generate_legacy(prompt, max_tokens=max_tokens, temperature=temperature, model=model)


def _gemini_generate_legacy(prompt, max_tokens=1024, temperature=0.0, model="gemini-2.5-flash-lite"):
    """Module-level Gemini call (gemini-2.5-flash-lite). Returns the response
    text or None on failure. Supports classic (AIza...) and OAuth-style keys.
    Kept as a fallback if no Anthropic key is configured."""
    import urllib.request as _ureq
    import json as _json
    import time as _time
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key or not prompt:
        return None
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    base_url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + model + ":generateContent"
    )
    data = _json.dumps(body).encode("utf-8")
    attempts = []
    if api_key.startswith("AIza"):
        attempts.append((base_url + "?key=" + api_key, None))
    else:
        attempts.append((base_url, {"Authorization": "Bearer " + api_key}))
        attempts.append((base_url, {"x-goog-api-key": api_key}))
        attempts.append((base_url + "?key=" + api_key, None))
    for url, headers in attempts:
        for retry in range(4):
            try:
                req = _ureq.Request(url, data=data, method="POST")
                req.add_header("Content-Type", "application/json")
                for h, v in (headers or {}).items():
                    req.add_header(h, v)
                with _ureq.urlopen(req, timeout=8) as resp:
                    rd = _json.loads(resp.read().decode("utf-8"))
                cands = rd.get("candidates") or []
                if not cands:
                    break
                parts = cands[0].get("content", {}).get("parts") or []
                if not parts:
                    break
                txt = (parts[0].get("text") or "").strip()
                if txt:
                    return txt
                break
            except Exception as e:
                es = str(e)
                # 429 = quota/rate limit exhausted. Retrying within a request is
                # pointless (the quota won't free up in a few seconds) and it
                # pins the worker, which can take the whole site down when many
                # DMs arrive at once. So on 429 we bail out immediately and let
                # the caller proceed without Gemini. Only retry truly transient
                # server errors (503/500/502) and timeouts, briefly.
                if "429" in es:
                    return None
                transient = ("503" in es or "500" in es
                             or "timed out" in es.lower() or "502" in es)
                if transient and retry < 2:
                    _time.sleep(1 + retry)
                    continue
                break
    return None


# ---------------------------------------------------------------------------
# SCAN PAGES
# ---------------------------------------------------------------------------

@login_required(login_url="/login/")
def shipping_scan(request):
    open_order = ShippingOrder.objects.filter(status=ShippingOrder.OPEN).first()
    return render(request, "inventory/shipping_scan.html", {"open_order": open_order})

@login_required(login_url="/login/")
def reception_scan(request):
    return render(request, "inventory/reception_scan.html", {})

@login_required(login_url="/login/")
def return_scan(request):
    return render(request, "inventory/return_scan.html", {})


# ---- Internal sale (employee / friend) -------------------------------------

@login_required(login_url="/login/")
def internal_sale_view(request):
    """Page where admin scans a unit barcode and sells it at cost price (or custom)."""
    if not request.user.is_staff:
        return HttpResponseForbidden("Accès réservé aux administrateurs.")
    return render(request, "inventory/internal_sale.html", {})


@csrf_exempt
@require_POST
@login_required(login_url="/login/")
def api_internal_sale_lookup(request):
    """Look up a ProductUnit by barcode. Returns its info + prices."""
    if not request.user.is_staff:
        return JsonResponse({"status": "error", "message": "Permission refusée."}, status=403)
    data = json.loads(request.body)
    barcode = (data.get("barcode") or "").strip().upper()
    if not barcode:
        return JsonResponse({"status": "error", "message": "Code-barres vide."})
    try:
        unit = ProductUnit.objects.select_related("variant", "variant__product").get(barcode=barcode)
    except ProductUnit.DoesNotExist:
        return JsonResponse({"status": "error", "message": f"Unité {barcode} introuvable."})

    product = unit.variant.product
    return JsonResponse({
        "status": "ok",
        "unit": {
            "barcode": unit.barcode,
            "status_raw": unit.status,
            "product_name": product.name,
            "color_label": unit.variant.color_label or "",
            "size": unit.size or "",
            "buy_price": str(product.buy_price),
            "sell_price": str(product.sell_price),
        }
    })


@csrf_exempt
@require_POST
@login_required(login_url="/login/")
def api_internal_sale_confirm(request):
    """Mark a unit as PAID at a custom price. Creates StockMovement + AuditLog."""
    if not request.user.is_staff:
        return JsonResponse({"status": "error", "message": "Permission refusée."}, status=403)
    from .models import log_action, AuditLog
    data = json.loads(request.body)
    barcode = (data.get("barcode") or "").strip().upper()
    sale_price_raw = (data.get("sale_price") or "").strip()
    note = (data.get("note") or "").strip()

    if not barcode:
        return JsonResponse({"status": "error", "message": "Code-barres vide."})
    try:
        sale_price = Decimal(sale_price_raw)
    except Exception:
        return JsonResponse({"status": "error", "message": "Prix invalide."})

    try:
        unit = ProductUnit.objects.select_related("variant", "variant__product").get(barcode=barcode)
    except ProductUnit.DoesNotExist:
        return JsonResponse({"status": "error", "message": f"Unité {barcode} introuvable."})

    if unit.status == ProductUnit.PAID:
        return JsonResponse({"status": "error", "message": f"Unité {barcode} déjà vendue."})
    if unit.status == ProductUnit.SHIPPED:
        return JsonResponse({"status": "error", "message": f"Unité {barcode} déjà expédiée — utilise le scan paiement normal."})

    old_status = unit.status
    unit.status = ProductUnit.PAID
    unit.save(update_fields=["status"])

    StockMovement.objects.create(
        unit=unit,
        movement_type=StockMovement.PAID,
        reference=f"VENTE INTERNE - {note}" if note else "VENTE INTERNE",
        user=request.user,
    )

    log_action(
        request.user, AuditLog.OTHER,
        description=(
            f"Vente interne : {unit.variant.product.name} ({unit.variant.color_label or '?'}, "
            f"taille {unit.size or '?'}) vendu à {sale_price} DT"
            + (f" — Note: {note}" if note else "")
            + f" (ancien statut: {old_status})"
        ),
        request=request,
        target_unit_barcode=barcode,
    )

    return JsonResponse({
        "status": "ok",
        "message": f"Unité {barcode} vendue à {sale_price} DT.",
        "product_name": unit.variant.product.name,
        "color": unit.variant.color_label or "",
        "size": unit.size or "",
    })


@login_required(login_url="/login/")
def stock_value(request):
    if not request.user.is_staff:
        return HttpResponseForbidden("Acces reserve aux administrateurs.")

    # Group by PRODUCT (sum across all variants/colors/sizes).
    from collections import defaultdict
    variants = ProductVariant.objects.select_related("product").prefetch_related("units")
    agg = defaultdict(lambda: {
        "product": None,
        "in_stock": 0,
        "shipped": 0,
        "returned": 0,
        "total_units": 0,
        "buy_total": Decimal("0"),
        "sell_total": Decimal("0"),
    })
    total_buy        = Decimal("0")
    total_sell       = Decimal("0")
    total_buy_shipped  = Decimal("0")
    total_sell_shipped = Decimal("0")

    for variant in variants:
        in_stock       = variant.units.filter(status=ProductUnit.IN_STOCK).count()
        shipped        = variant.units.filter(status=ProductUnit.SHIPPED).count()
        pending_return = variant.units.filter(status=ProductUnit.RETURNED).count()
        total_units    = in_stock + shipped + pending_return
        if total_units == 0:
            continue
        buy  = variant.product.buy_price  * total_units
        sell = variant.product.sell_price * total_units
        buy_shipped  = variant.product.buy_price  * (shipped + pending_return)
        sell_shipped = variant.product.sell_price * (shipped + pending_return)
        total_buy          += buy
        total_sell         += sell
        total_buy_shipped  += buy_shipped
        total_sell_shipped += sell_shipped
        # Aggregate into per-product row
        row = agg[variant.product_id]
        row["product"] = variant.product
        row["in_stock"]    += in_stock
        row["shipped"]     += shipped
        row["returned"]    += pending_return
        row["total_units"] += total_units
        row["buy_total"]   += buy
        row["sell_total"]  += sell
        # Pick the first variant image we find — that's our product thumbnail
        if not row.get("image") and variant.image:
            row["image"] = variant.image
            row["image_color"] = variant.color_name or ""

    rows = sorted(agg.values(), key=lambda r: r["product"].name.lower())
    return render(request, "inventory/stock_value.html", {
        "rows": rows,
        "total_buy": total_buy,
        "total_sell": total_sell,
        "potential_profit": total_sell - total_buy,
        "total_buy_shipped": total_buy_shipped,
        "total_sell_shipped": total_sell_shipped,
    })


# ---------------------------------------------------------------------------
# SCAN APIs
# ---------------------------------------------------------------------------

@csrf_exempt
@require_POST
def api_scan_shipping(request):
    data = json.loads(request.body)
    barcode = data.get("barcode", "").strip()
    if not barcode:
        return JsonResponse({"status": "error", "message": "Barcode vide."}, status=400)
    result = handle_shipping_scan(barcode, user=_user_for_request(request))

    # Audit — record what kind of scan this resolved to.
    from .models import log_action, AuditLog
    rtype = result.get("type", "unknown")
    if rtype == "bordereau":
        log_action(
            request.user, AuditLog.SCAN_SHIPPING,
            description=f"Bordereau scanné : {barcode}",
            request=request, target_order_barcode=barcode,
        )
    elif rtype == "unit":
        unit_bc = (result.get("unit") or {}).get("barcode", "")
        order_bc = (result.get("order") or {}).get("bordereau_barcode", "")
        log_action(
            request.user, AuditLog.SCAN_SHIPPING,
            description=f"Unité {unit_bc} scannée vers {order_bc}",
            request=request, target_unit_barcode=unit_bc, target_order_barcode=order_bc,
        )
    elif result.get("status") == "error":
        log_action(
            request.user, AuditLog.SCAN_SHIPPING,
            description=f"Scan refusé : {barcode} — {result.get('message','')[:200]}",
            request=request, target_unit_barcode=barcode,
        )
    return JsonResponse(result)


@login_required(login_url="/login/")
def api_get_order_state(request, pk):
    """Return the current saved unit list for an order — used by the frontend
    to recover after a scan failure or tab focus change. Server is source of truth."""
    try:
        order = ShippingOrder.objects.prefetch_related(
            "items__unit__variant__product"
        ).get(pk=pk)
    except ShippingOrder.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Ordre introuvable."}, status=404)

    units = []
    total = Decimal("0")
    for item in order.items.all():
        unit = item.unit
        if not unit:
            continue
        variant = unit.variant
        product = variant.product
        units.append({
            "barcode": unit.barcode,
            "size": unit.size,
            "status": unit.status,
            "product_name": product.name,
            "color_label": variant.color_label,
            "sell_price": str(product.sell_price),
            "image_url": variant.image.url if variant.image else None,
        })
        total += product.sell_price

    return JsonResponse({
        "status": "ok",
        "order": {
            "id": order.id,
            "bordereau_barcode": order.bordereau_barcode,
            "status": order.status,
            "unit_count": len(units),
            "total": str(total),
        },
        "units": units,
    })


@csrf_exempt
@require_POST
def api_scan_reception(request):
    data = json.loads(request.body)
    barcode = data.get("barcode", "").strip()
    if not barcode:
        return JsonResponse({"status": "error", "message": "Barcode vide."}, status=400)
    result = handle_stock_scan(barcode, user=_user_for_request(request))

    from .models import log_action, AuditLog
    if result.get("status") == "ok":
        unit_bc = (result.get("unit") or {}).get("barcode", barcode)
        log_action(
            request.user, AuditLog.SCAN_RECEPTION,
            description=f"Unité {unit_bc} ajoutée au stock",
            request=request, target_unit_barcode=unit_bc,
        )
    else:
        log_action(
            request.user, AuditLog.SCAN_RECEPTION,
            description=f"Scan stock refusé : {barcode} — {result.get('message','')[:200]}",
            request=request, target_unit_barcode=barcode,
        )
    return JsonResponse(result)


@csrf_exempt
@require_POST
def api_remove_from_order(request):
    """Remove a unit from the currently open order."""
    from .models import log_action, AuditLog
    data = json.loads(request.body)
    barcode = data.get("barcode", "").strip().upper()
    open_order = ShippingOrder.objects.filter(status=ShippingOrder.OPEN).first()
    if not open_order:
        return JsonResponse({"status": "error", "message": "Aucun ordre ouvert."})
    try:
        unit = ProductUnit.objects.get(barcode=barcode)
        item = OrderItem.objects.get(order=open_order, unit=unit)
        item.delete()
        unit.status = ProductUnit.IN_STOCK
        unit.save()
        log_action(
            request.user, AuditLog.EDIT,
            description=f"Unité {barcode} retirée de l'ordre ouvert {open_order.bordereau_barcode}",
            request=request,
            target_unit_barcode=barcode,
            target_order_barcode=open_order.bordereau_barcode,
        )
        return JsonResponse({
            "status": "ok",
            "message": f"{barcode} retiré de l'ordre.",
            "unit_count": open_order.items.count(),
        })
    except (ProductUnit.DoesNotExist, OrderItem.DoesNotExist):
        return JsonResponse({"status": "error", "message": f"Unité {barcode} non trouvée dans cet ordre."})


def login_view(request):
    if request.user.is_authenticated:
        return redirect('home')
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        next_url = request.POST.get('next', '/')
        user = authenticate(request, username=username, password=password)
        if user:
            login(request, user)
            return redirect(next_url or 'home')
        return render(request, 'inventory/login.html', {'form': type('F', (), {'errors': True})(), 'next': next_url})
    return render(request, 'inventory/login.html', {'next': request.GET.get('next', '/')})


def logout_view(request):
    logout(request)
    return redirect('login')



def _do_return_unit(unit, user=None):
    variant = unit.variant
    reconciliation = None
    order_item = unit.order_items.select_related("order").order_by("-scanned_at").first()
    if order_item and order_item.order.status == ShippingOrder.PAID:
        order = order_item.order
        refund_amount = variant.product.sell_price
        new_amount = max(Decimal("0"), (order.amount_collected or Decimal("0")) - refund_amount)
        order.amount_collected = new_amount
        order.save()
        try:
            payment = order.payment
            payment.amount_collected = new_amount
            payment.amount_expected = max(Decimal("0"), payment.amount_expected - refund_amount)
            payment.save()
        except Exception:
            pass
        reconciliation = {
            "order_bordereau": order.bordereau_barcode,
            "refund_amount": str(refund_amount),
            "new_order_amount": str(new_amount),
            "message": f"Ordre {order.bordereau_barcode} — montant ajuste de -{refund_amount} TND (nouveau total : {new_amount} TND)",
        }
    unit.status = ProductUnit.RETURNED
    unit.save()
    StockMovement.objects.create(unit=unit, movement_type=StockMovement.RETURNED, reference="RETOUR", user=user)
    if order_item:
        order_item.status_at_payment = ProductUnit.RETURNED
        order_item.save(update_fields=["status_at_payment"])
    # Update order status based on remaining units (handles paid / livre / closed)
    if order_item and order_item.order_id:
        order_item.order.refresh_from_db()
        _update_order_return_status(order_item.order)
    unit_data = {
        "barcode": unit.barcode, "size": unit.size,
        "product_name": variant.product.name, "color_label": variant.color_label,
        "sell_price": str(variant.product.sell_price),
        "image_url": variant.image.url if variant.image else None,
    }
    return unit_data, reconciliation


def _update_order_return_status(order):
    """Update order status after a unit return."""
    items = list(order.items.select_related("unit").all())
    if not items:
        return
    statuses = [item.unit.status for item in items]
    all_returned = all(s == ProductUnit.RETURNED for s in statuses)
    any_returned = any(s == ProductUnit.RETURNED for s in statuses)

    if order.status in (ShippingOrder.PAID, ShippingOrder.PARTIAL_PAID):
        order.status = ShippingOrder.PARTIAL_PAID if not all_returned else ShippingOrder.RETURNED
    elif order.status in (ShippingOrder.LIVRE, ShippingOrder.PARTIAL_LIVRE):
        order.status = ShippingOrder.PARTIAL_LIVRE if not all_returned else ShippingOrder.RETURNED
    elif order.status == ShippingOrder.CLOSED:
        if all_returned:
            order.status = ShippingOrder.RETURNED
        elif any_returned:
            order.status = ShippingOrder.PARTIAL_RETURNED
    order.save(update_fields=["status"])

    # Propagate to the linked v2 Order: if this v1 ShippingOrder is now
    # RETURNED or PARTIAL_RETURNED and is linked to a v2 Order, move that v2
    # Order to RETURNED ("Retourné", final). It then drops out of the active
    # lists and the Navex sync.
    if order.status in (ShippingOrder.RETURNED, ShippingOrder.PARTIAL_RETURNED):
        try:
            from .models import Order as _V2Order, log_action, AuditLog
            v2 = getattr(order, "order", None)
            if v2 is not None and v2.status != _V2Order.RETURNED:
                old_label = dict(_V2Order.STATUS_CHOICES).get(v2.status, v2.status)
                v2.status = _V2Order.RETURNED
                v2.save(update_fields=["status", "updated_at"])
                log_action(
                    None, AuditLog.STATUS_CHANGE,
                    description=f"Auto: commande v2 #{v2.id} {old_label} → 'Retourné' "
                                f"(scan retour v1, bordereau {order.bordereau_barcode})",
                    target_model="Order", target_id=v2.id,
                )
        except Exception:
            # Never let v2 propagation break the v1 return flow.
            pass


@csrf_exempt
@require_POST
def api_scan_return(request):
    from .barcode_parser import is_bordereau_barcode
    from .models import log_action, AuditLog, Order, ExchangeReturnItem
    data = json.loads(request.body)
    barcode = data.get("barcode", "").strip().upper()
    if not barcode:
        return JsonResponse({"status": "error", "message": "Barcode vide."}, status=400)

    # CASE 1: Check if this is an exchange return code (code_echange from Navex)
    # An exchange's return colis has a different barcode from the original push
    # bordereau. It's stored in Order.navex_return_barcode when Navex assigns it.
    # We check this FIRST, before the regular ShippingOrder lookup.
    try:
        exchange_order = Order.objects.prefetch_related(
            "return_items__variant__product"
        ).get(navex_return_barcode=barcode)
    except Order.DoesNotExist:
        exchange_order = None
    except Order.MultipleObjectsReturned:
        # Defensive: if somehow two orders share the same return barcode, take the most recent
        exchange_order = Order.objects.filter(navex_return_barcode=barcode).order_by("-created_at").first()

    if exchange_order is not None:
        # Build the items list from ExchangeReturnItem (the products the customer
        # said they would return when the exchange was created).
        return_items = exchange_order.return_items.all()
        if not return_items.exists():
            log_action(
                request.user, AuditLog.SCAN_RETURN,
                description=f"Retour échange : code {barcode} (commande #{exchange_order.id}) mais aucun article à retourner enregistré",
                request=request, target_order_barcode=barcode,
            )
            return JsonResponse({
                "status": "error",
                "message": f"Échange #{exchange_order.id} reconnu, mais aucun article à retourner n'a été enregistré.",
                "code": "EXCHANGE_NO_RETURNS",
            })

        # Format items in the same shape as the v1 scan_return response.
        # We don't have a physical ProductUnit here (the scan of the unit will
        # come later — at this stage we just show what's EXPECTED).
        items_data = []
        for ri in return_items:
            variant = ri.variant or (ri.unit.variant if ri.unit else None)
            product = variant.product if variant else None
            items_data.append({
                # Show the real physical barcode when the return item is linked to
                # a specific unit; otherwise a placeholder until the unit is scanned.
                "barcode": ri.unit.barcode if ri.unit else f"RETURN-{ri.id}",
                "size": ri.size or (ri.unit.size if ri.unit else ""),
                "status": "pending_return",  # Custom status: still waiting for scan
                "product_name": product.name if product else (ri.product_name_snapshot or "?"),
                "color_label": variant.color_label if variant else "",
                "sell_price": str(product.sell_price) if product else "0",
                "image_url": variant.image.url if variant and variant.image else None,
                "exchange_return_item_id": ri.id,
            })

        log_action(
            request.user, AuditLog.SCAN_RETURN,
            description=f"Code d'échange retour scanné : {barcode} → commande échange #{exchange_order.id} ({len(items_data)} article(s) à recevoir)",
            request=request, target_order_barcode=barcode,
        )
        return JsonResponse({
            "status": "ok",
            "type": "order_multiple",
            "order_bordereau": barcode,
            "order_id": exchange_order.id,
            "is_exchange": True,
            "exchange_of_id": exchange_order.exchange_of_id,
            "items": items_data,
        })

    # CASE 2: Regular v1 ShippingOrder lookup (existing logic, unchanged)
    if is_bordereau_barcode(barcode):
        try:
            order = ShippingOrder.objects.prefetch_related("items__unit__variant__product").get(bordereau_barcode=barcode)
        except ShippingOrder.DoesNotExist:
            log_action(
                request.user, AuditLog.SCAN_RETURN,
                description=f"Retour : bordereau introuvable {barcode}",
                request=request, target_order_barcode=barcode,
            )
            return JsonResponse({"status": "error", "message": f"Aucun ordre trouvé pour : {barcode}", "code": "ORDER_NOT_FOUND", "barcode": barcode})
        items = order.items.select_related("unit__variant__product")
        # Use the snapshotted status (display_status) — same source the UI uses
        # to show 'Expédié' on each item. This matches user expectation: if the
        # UI says it's expedié, it should be returnable. Also defensive against
        # the rare case where a unit's live status was changed by another flow
        # (e.g. unit reused in another order) leaving the snapshot intact.
        # We also accept legacy items where snapshots are missing (display_status
        # falls back to unit.status).
        RETURNABLE_STATUSES = {"shipped", "paid", "expédié", "expedie",
                               "early_return", "at_depot"}
        returnable = [i for i in items if (i.display_status or "").strip().lower() in RETURNABLE_STATUSES]
        if not returnable:
            # Build a clearer error message — list what state each item is in
            # so the user understands WHY nothing is returnable.
            if not items:
                msg = "Cet ordre n'a aucun article."
            else:
                # Map statuses to human-readable labels
                status_labels = {
                    "in_stock": "En stock",
                    "shipped": "Expédié",
                    "paid": "Payé",
                    "returned": "Déjà retourné",
                    "defective": "Défectueux",
                    "early_return": "Retour anticipé",
                    "at_depot": "Retour en dépôt Navex",
                }
                item_states = []
                for i in items:
                    s = (i.display_status or "").strip().lower()
                    label = status_labels.get(s, s or "inconnu")
                    item_states.append(f"{i.unit.barcode} : {label}")
                msg = "Aucune unité retournable. État des articles :\n" + "\n".join(item_states)
            return JsonResponse({"status": "error", "message": msg, "code": "NOTHING_RETURNABLE"})
        items_data = [{"barcode": i.unit.barcode, "size": i.unit.size, "status": i.unit.status,
                       "product_name": i.unit.variant.product.name, "color_label": i.unit.variant.color_label,
                       "sell_price": str(i.unit.variant.product.sell_price),
                       "image_url": i.unit.variant.image.url if i.unit.variant.image else None}
                      for i in items]
        # Always open the modal regardless of unit count.
        # The "auto-return on single unit" shortcut was a footgun: a single
        # accidental scan would mark an item returned with no confirmation.
        # Now the worker must scan the actual product barcode inside the modal,
        # which guarantees they have the physical item in hand.
        log_action(
            request.user, AuditLog.SCAN_RETURN,
            description=f"Retour ouvert pour {barcode} ({len(returnable)} retournable(s))",
            request=request, target_order_barcode=barcode,
        )
        return JsonResponse({"status": "ok", "type": "order_multiple",
                             "order_bordereau": order.bordereau_barcode,
                             "order_id": order.id, "items": items_data})

    try:
        unit = ProductUnit.objects.select_related("variant__product").get(barcode=barcode)
    except ProductUnit.DoesNotExist:
        log_action(
            request.user, AuditLog.SCAN_RETURN,
            description=f"Retour : unité introuvable {barcode}",
            request=request, target_unit_barcode=barcode,
        )
        return JsonResponse({"status": "error", "message": f"Unite introuvable : {barcode}"})
    if unit.status not in (ProductUnit.SHIPPED, ProductUnit.PAID,
                           ProductUnit.EARLY_RETURN, ProductUnit.AT_DEPOT):
        msgs = {
            ProductUnit.IN_STOCK: "déjà en stock",
            ProductUnit.RETURNED: "déjà retournée",
        }
        log_action(
            request.user, AuditLog.SCAN_RETURN,
            description=f"Retour refusé pour {barcode} : statut {unit.get_status_display()}",
            request=request, target_unit_barcode=barcode,
        )
        return JsonResponse({"status": "error", "message": f"Impossible — {msgs.get(unit.status, unit.get_status_display())}."})
    unit_data, reconciliation = _do_return_unit(unit, user=_user_for_request(request))
    log_action(
        request.user, AuditLog.SCAN_RETURN,
        description=f"Unité {unit_data['barcode']} retournée",
        request=request, target_unit_barcode=unit_data["barcode"],
    )
    return JsonResponse({"status": "ok", "type": "unit_returned",
                         "message": f"{unit_data['product_name']} {unit_data['color_label']} taille {unit_data['size']} retourné.",
                         "unit": unit_data, "reconciliation": reconciliation})


@csrf_exempt
@require_POST
def api_exchange_mark_received(request):
    """Phase 2.5b: mark exchange return item(s) as RECEIVED when the return
    colis from an exchange is scanned/confirmed.

    Body (either form):
      {"exchange_return_item_id": 12}                 # mark one item received
      {"exchange_return_item_ids": [12, 13]}          # mark several
      {"missing_ids": [14]}                           # optionally flag missing

    For each item marked received: if it is linked to a physical ProductUnit,
    that unit is returned to stock (status IN_STOCK + a StockMovement), mirroring
    the normal return flow.
    """
    from .models import log_action, AuditLog, ExchangeReturnItem, ProductUnit, StockMovement
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "JSON invalide."}, status=400)

    ids = data.get("exchange_return_item_ids")
    if not ids:
        single = data.get("exchange_return_item_id")
        ids = [single] if single else []
    missing_ids = data.get("missing_ids") or []

    if not ids and not missing_ids:
        return JsonResponse({"status": "error", "message": "Aucun article à marquer."}, status=400)

    received_count = 0
    restocked = 0
    with transaction.atomic():
        # Mark received
        for ri in ExchangeReturnItem.objects.select_related("unit").filter(id__in=[int(i) for i in ids]):
            if ri.status != ExchangeReturnItem.RECEIVED_OK:
                ri.status = ExchangeReturnItem.RECEIVED_OK
                ri.save(update_fields=["status", "updated_at"])
                received_count += 1
                # If we know the physical unit, mark it returned (back from the
                # customer, sellable but flagged as a return for visibility).
                if ri.unit_id:
                    unit = ri.unit
                    if unit.status != ProductUnit.RETURNED:
                        unit.status = ProductUnit.RETURNED
                        unit.save(update_fields=["status"])
                        StockMovement.objects.create(
                            unit=unit, movement_type=StockMovement.RECEIVED,
                            reference=f"RETOUR ÉCHANGE #{ri.exchange_order_id}",
                            user=_user_for_request(request),
                        )
                        restocked += 1
        # Mark missing
        marked_missing = 0
        for ri in ExchangeReturnItem.objects.filter(id__in=[int(i) for i in missing_ids]):
            if ri.status != ExchangeReturnItem.RECEIVED_MISSING:
                ri.status = ExchangeReturnItem.RECEIVED_MISSING
                ri.save(update_fields=["status", "updated_at"])
                marked_missing += 1

    log_action(
        request.user, AuditLog.SCAN_RETURN,
        description=f"Retour échange : {received_count} article(s) reçu(s), {restocked} remis en stock"
                    + (f", {marked_missing} manquant(s)" if missing_ids else ""),
        request=request,
    )
    return JsonResponse({
        "status": "ok",
        "received": received_count,
        "restocked": restocked,
        "missing": len(missing_ids),
    })


@csrf_exempt
@require_POST
def api_return_multiple(request):
    from .models import log_action, AuditLog
    data = json.loads(request.body)
    barcodes = data.get("barcodes", [])
    if not barcodes:
        return JsonResponse({"status": "error", "message": "Aucune unité sélectionnée."})
    returned_units = []
    reconciliations = []
    for barcode in barcodes:
        try:
            unit = ProductUnit.objects.select_related("variant__product").get(barcode=barcode)
            if unit.status in (ProductUnit.SHIPPED, ProductUnit.PAID,
                               ProductUnit.EARLY_RETURN, ProductUnit.AT_DEPOT):
                unit_data, reconciliation = _do_return_unit(unit, user=_user_for_request(request))
                returned_units.append(unit_data)
                if reconciliation:
                    reconciliations.append(reconciliation)
        except ProductUnit.DoesNotExist:
            pass
    if not returned_units:
        return JsonResponse({"status": "error", "message": "Aucune unité retournée."})
    log_action(
        request.user, AuditLog.SCAN_RETURN,
        description=f"Retour multi-unité : {len(returned_units)} unité(s) — {', '.join(u['barcode'] for u in returned_units[:5])}{'...' if len(returned_units) > 5 else ''}",
        request=request,
    )
    combined = {"message": " | ".join(r["message"] for r in reconciliations)} if reconciliations else None
    return JsonResponse({"status": "ok", "message": f"{len(returned_units)} unité(s) retournée(s).",
                         "returned_units": returned_units, "reconciliation": combined})


@csrf_exempt
@require_POST
def api_scan_payment(request):
    """
    Scan a payment barcode from the shipping company slip.
    - If it matches a closed order's payment_barcode → return order details for review
    - If another payment barcode is scanned → mark previous as fully paid
    """
    from .models import log_action, AuditLog
    data = json.loads(request.body)
    barcode = data.get("barcode", "").strip()
    if not barcode:
        return JsonResponse({"status": "error", "message": "Barcode vide."})

    # Check if there's a pending payment order being reviewed
    pending_order_id = data.get("pending_order_id")

    # If another barcode scanned while reviewing an order → mark it paid
    if pending_order_id:
        try:
            prev_order = ShippingOrder.objects.get(pk=pending_order_id)
            if prev_order.status == ShippingOrder.CLOSED:
                prev_order.status = ShippingOrder.PAID
                prev_order.paid_at = prev_order.navex_paid_detected_at or timezone.now()
                prev_order.save()
                # Mark all shipped items as sold
                for item in prev_order.items.select_related("unit"):
                    if item.unit.status == ProductUnit.SHIPPED:
                        item.unit.status = ProductUnit.PAID
                        item.unit.save()
                        StockMovement.objects.create(
                            unit=item.unit, movement_type=StockMovement.PAID,
                            reference=prev_order.bordereau_barcode, user=_user_for_request(request))
                log_action(
                    request.user, AuditLog.PAYMENT,
                    description=f"Ordre {prev_order.bordereau_barcode} marqué payé (auto, scan suivant)",
                    request=request,
                    target_order_barcode=prev_order.bordereau_barcode,
                    target_model="ShippingOrder", target_id=prev_order.id,
                )
        except ShippingOrder.DoesNotExist:
            pass

    # Find the order for the new barcode
    try:
        order = ShippingOrder.objects.prefetch_related(
            "items__unit__variant__product__variants"
        ).get(bordereau_barcode=barcode)
    except ShippingOrder.DoesNotExist:
        # Try payment_barcode field too
        try:
            order = ShippingOrder.objects.prefetch_related(
                "items__unit__variant__product__variants"
            ).get(payment_barcode=barcode)
        except ShippingOrder.DoesNotExist:
            return JsonResponse({"status": "error", "message": f"Aucun ordre trouvé pour : {barcode}", "code": "NOT_FOUND"})

    if order.status == ShippingOrder.PAID:
        return JsonResponse({"status": "error", "message": f"Cet ordre est déjà marqué comme payé.", "code": "ALREADY_PAID"})

    items_data = []
    for item in order.items.select_related("unit__variant__product"):
        unit = item.unit
        variant = unit.variant
        # Only shipped units can be marked as sold
        # returned/pending_return → locked as refused
        can_be_sold = unit.status == ProductUnit.SHIPPED
        auto_refused = unit.status in (ProductUnit.RETURNED, ProductUnit.RETURNED)
        items_data.append({
            "order_item_id": item.id,
            "barcode": unit.barcode,
            "size": unit.size,
            "product_name": variant.product.name,
            "color_label": variant.color_label,
            "sell_price": str(variant.product.sell_price),
            "status": unit.status,
            "can_be_sold": can_be_sold,
            "auto_refused": auto_refused,
            "image_url": variant.image.url if variant.image else None,
        })

    total = sum(Decimal(i["sell_price"]) for i in items_data)

    return JsonResponse({
        "status": "ok",
        "type": "payment_review",
        "order": {
            "id": order.id,
            "bordereau_barcode": order.bordereau_barcode,
            "status": order.status,
            "opened_at": order.opened_at.strftime("%d/%m/%Y %H:%M"),
            "unit_count": order.unit_count,
            "expected_amount": str(total + 7),
            "shipping_fee": "7.000",
        },
        "items": items_data,
    })


@csrf_exempt
@require_POST
def api_confirm_payment(request):
    """Confirm payment with optional modifications (refused items, adjusted price)."""
    from .models import log_action, AuditLog
    data = json.loads(request.body)
    order_id = data.get("order_id")
    sold_barcodes = data.get("sold_barcodes", [])   # items client accepted
    refused_barcodes = data.get("refused_barcodes", [])  # items client refused
    amount_collected = Decimal(str(data.get("amount_collected", "0")))
    notes = data.get("notes", "")

    try:
        order = ShippingOrder.objects.get(pk=order_id)
    except ShippingOrder.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Ordre introuvable."})

    # Mark sold items — skip any returned units (they cannot be paid directly)
    for barcode in sold_barcodes:
        try:
            unit = ProductUnit.objects.get(barcode=barcode)
            if unit.status == ProductUnit.RETURNED:
                continue  # Cannot pay a returned unit
            unit.status = ProductUnit.PAID
            unit.save()
            StockMovement.objects.create(unit=unit, movement_type=StockMovement.PAID, reference=order.bordereau_barcode, user=_user_for_request(request))
            OrderItem.objects.filter(order=order, unit=unit).update(status_at_payment=ProductUnit.PAID)
        except ProductUnit.DoesNotExist:
            pass

    # Mark refused items as pending_return
    for barcode in refused_barcodes:
        try:
            unit = ProductUnit.objects.get(barcode=barcode)
            unit.status = ProductUnit.RETURNED
            unit.save()
            StockMovement.objects.create(unit=unit, movement_type=StockMovement.RETURNED, reference=order.bordereau_barcode, user=_user_for_request(request))
            # Save snapshot on OrderItem
            OrderItem.objects.filter(order=order, unit=unit).update(status_at_payment=ProductUnit.RETURNED)
        except ProductUnit.DoesNotExist:
            pass

    # Calculate expected
    sold_total = sum(
        ProductUnit.objects.get(barcode=b).variant.product.sell_price
        for b in sold_barcodes
        if ProductUnit.objects.filter(barcode=b).exists()
    )
    expected = sold_total + 7

    # Create payment record
    Payment.objects.update_or_create(
        order=order,
        defaults={
            "amount_expected": expected,
            "amount_collected": amount_collected,
            "shipping_fee": Decimal("7"),
            "notes": notes,
        }
    )

    # Mark order as paid — also fix any remaining shipped units
    order.status = ShippingOrder.PAID
    order.paid_at = order.navex_paid_detected_at or timezone.now()
    order.amount_collected = amount_collected
    order.notes = notes
    order.save()

    # Safety: mark any remaining shipped units as paid
    for item in order.items.select_related("unit"):
        if item.unit.status == ProductUnit.SHIPPED:
            item.unit.status = ProductUnit.PAID
            item.unit.save()
            OrderItem.objects.filter(pk=item.pk).update(status_at_payment=ProductUnit.PAID)

    pending_count = len(refused_barcodes)
    log_action(
        request.user, AuditLog.PAYMENT,
        description=f"Paiement confirmé pour {order.bordereau_barcode} : {amount_collected} TND ({len(sold_barcodes)} vendu, {pending_count} refusé)",
        request=request,
        target_order_barcode=order.bordereau_barcode,
        target_model="ShippingOrder", target_id=order.id,
    )

    # If linked to a Shopify-originating v2 Order, mark it paid on Shopify too.
    try:
        v2 = order.order
        if v2 and v2.notes:
            shopify_id = _extract_shopify_order_id_from_notes(v2.notes)
            if shopify_id:
                ok_sh, sh_resp = _shopify_mark_paid(shopify_id, amount_collected, "TND")
                if ok_sh:
                    log_action(
                        request.user, AuditLog.PAYMENT,
                        description=f"Shopify : commande {shopify_id} marquée payée ({amount_collected} TND, Order v2 #{v2.id})",
                        request=request,
                        target_model="Order", target_id=v2.id,
                    )
                else:
                    err_msg = sh_resp.get("_error") if isinstance(sh_resp, dict) else str(sh_resp)
                    log_action(
                        request.user, AuditLog.PAYMENT,
                        description=f"Shopify : ÉCHEC mark paid pour {shopify_id} (Order v2 #{v2.id}) — {err_msg}",
                        request=request,
                        target_model="Order", target_id=v2.id,
                    )
    except Exception:
        pass

    return JsonResponse({
        "status": "ok",
        "message": f"Paiement confirmé. {len(sold_barcodes)} vendu(s), {pending_count} en attente de retour physique.",
        "pending_returns": pending_count,
        "amount_collected": str(amount_collected),
        "amount_expected": str(expected),
    })


@csrf_exempt
@require_POST
def api_close_order(request):
    """Manually close the currently open order with an expected amount."""
    from .models import log_action, AuditLog
    data = json.loads(request.body)
    expected_amount = Decimal(str(data.get("expected_amount", "0")))

    open_order = ShippingOrder.objects.filter(status=ShippingOrder.OPEN).first()
    if not open_order:
        return JsonResponse({"status": "error", "message": "Aucun ordre ouvert."})

    if open_order.items.count() == 0:
        return JsonResponse({
            "status": "error",
            "message": f"Impossible de fermer l'ordre {open_order.bordereau_barcode} — aucune unité scannée ! Scannez au moins un produit avant de fermer."
        })

    open_order.status = ShippingOrder.CLOSED
    open_order.closed_at = timezone.now()
    open_order.amount_collected = expected_amount
    open_order.save()

    for item in open_order.items.select_related("unit"):
        item.unit.status = ProductUnit.SHIPPED
        item.unit.save()
        item.status_at_close = ProductUnit.SHIPPED
        item.save(update_fields=["status_at_close"])
        StockMovement.objects.create(
            unit=item.unit,
            movement_type=StockMovement.SHIPPED,
            reference=open_order.bordereau_barcode, user=_user_for_request(request))

    log_action(
        request.user, AuditLog.STATUS_CHANGE,
        description=f"Ordre {open_order.bordereau_barcode} fermé ({open_order.unit_count} unités, {expected_amount} TND)",
        request=request,
        target_order_barcode=open_order.bordereau_barcode,
        target_model="ShippingOrder", target_id=open_order.id,
    )

    return JsonResponse({
        "status": "ok",
        "message": f"Ordre {open_order.bordereau_barcode} fermé.",
        "order_id": open_order.id,
        "unit_count": open_order.unit_count,
        "expected_amount": str(expected_amount),
    })


@csrf_exempt
@require_POST
def api_update_order_amount(request, pk):
    """Update the amount collected for an order from order detail page."""
    from .models import log_action, AuditLog
    data = json.loads(request.body)
    amount = Decimal(str(data.get("amount_collected", "0")))
    try:
        order = ShippingOrder.objects.get(pk=pk)
    except ShippingOrder.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Ordre introuvable."})

    old_amount = order.amount_collected or Decimal("0")
    order.amount_collected = amount
    order.save()

    # Also update payment record if exists
    note = None
    try:
        payment = order.payment
        payment.amount_collected = amount
        payment.save()
    except Exception:
        pass

    diff = amount - old_amount
    if diff != 0:
        note = f"Montant {'augmenté' if diff > 0 else 'réduit'} de {abs(diff):.3f} TND (ancien: {old_amount:.3f} TND)"

    log_action(
        request.user, AuditLog.EDIT,
        description=f"Montant ordre {order.bordereau_barcode} : {old_amount} TND → {amount} TND",
        request=request,
        target_order_barcode=order.bordereau_barcode,
        target_model="ShippingOrder", target_id=order.id,
    )

    return JsonResponse({"status": "ok", "note": note})


@login_required(login_url="/login/")
def search(request):
    return render(request, "inventory/search.html", {})


def api_search(request):
    q = request.GET.get("q", "").strip()
    if not q:
        return JsonResponse({"units": [], "orders": []})

    units_data = []
    orders_data = []

    # Search units by barcode or product name
    from django.db.models import Q
    units = ProductUnit.objects.select_related(
        "variant__product", "variant"
    ).filter(
        Q(barcode__icontains=q) |
        Q(variant__product__name__icontains=q) |
        Q(variant__color_name__icontains=q) |
        Q(variant__color_label__icontains=q)
    )[:20]

    for unit in units:
        variant = unit.variant
        last_order_item = unit.order_items.select_related("order").order_by("-scanned_at").first()
        units_data.append({
            "barcode": unit.barcode,
            "size": unit.size,
            "status": unit.status,
            "status_label": unit.get_status_display(),
            "product_name": variant.product.name,
            "color_label": variant.color_label,
            "sell_price": str(variant.product.sell_price),
            "image_url": variant.image.url if variant.image else None,
            "created_at": unit.created_at.strftime("%d/%m/%Y"),
            "order_bordereau": last_order_item.order.bordereau_barcode if last_order_item else None,
            "order_id": last_order_item.order.id if last_order_item else None,
        })

    # Search orders by bordereau barcode
    orders = ShippingOrder.objects.prefetch_related(
        "items__unit__variant__product"
    ).filter(bordereau_barcode__icontains=q)[:10]

    for order in orders:
        items_data = []
        for item in order.items.select_related("unit__variant__product")[:6]:
            items_data.append({
                "product_name": item.unit.variant.product.name,
                "color_label": item.unit.variant.color_label,
                "size": item.unit.size,
                "image_url": item.unit.variant.image.url if item.unit.variant.image else None,
            })
        orders_data.append({
            "id": order.id,
            "bordereau_barcode": order.bordereau_barcode,
            "status": order.status,
            "opened_at": order.opened_at.strftime("%d/%m/%Y %H:%M"),
            "unit_count": order.unit_count,
            "items": items_data,
        })

    return JsonResponse({"units": units_data, "orders": orders_data})


@csrf_exempt
@require_POST
def api_fix_order_units(request, pk):
    """Resync unit statuses with their order status."""
    from .models import log_action, AuditLog
    try:
        order = ShippingOrder.objects.prefetch_related("items__unit").get(pk=pk)
    except ShippingOrder.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Ordre introuvable."})

    fixed = 0
    for item in order.items.select_related("unit"):
        unit = item.unit
        if order.status == ShippingOrder.PAID and unit.status == ProductUnit.SHIPPED:
            unit.status = ProductUnit.PAID
            unit.save()
            OrderItem.objects.filter(pk=item.pk).update(status_at_payment=ProductUnit.PAID)
            fixed += 1
        elif order.status == ShippingOrder.CLOSED and unit.status not in (ProductUnit.RETURNED, ProductUnit.PAID):
            unit.status = ProductUnit.SHIPPED
            unit.save()
            fixed += 1

    if fixed > 0:
        log_action(
            request.user, AuditLog.EDIT,
            description=f"Resync ordre {order.bordereau_barcode} : {fixed} unité(s) corrigée(s)",
            request=request,
            target_order_barcode=order.bordereau_barcode,
            target_model="ShippingOrder", target_id=order.id,
        )

    return JsonResponse({"status": "ok", "message": f"{fixed} unité(s) resynchronisée(s).", "fixed": fixed})


@csrf_exempt
@require_POST
def api_delete_order(request, pk):
    """Delete an order and return all units to in_stock."""
    from .models import log_action, AuditLog
    data = json.loads(request.body)
    confirmation = data.get("confirmation", "")
    if confirmation != "DELETE":
        return JsonResponse({"status": "error", "message": "Confirmation incorrecte."})
    try:
        order = ShippingOrder.objects.get(pk=pk)
    except ShippingOrder.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Ordre introuvable."})
    if order.status == ShippingOrder.PAID:
        return JsonResponse({"status": "error", "message": "Impossible de supprimer un ordre déjà payé."})
    # Capture key fields BEFORE delete so the audit row stays useful
    bordereau = order.bordereau_barcode
    unit_count = order.items.count()
    order_id = order.id
    # Return all units to in_stock
    for item in order.items.select_related("unit"):
        item.unit.status = ProductUnit.IN_STOCK
        item.unit.save()
        StockMovement.objects.create(
            unit=item.unit, movement_type=StockMovement.RECEIVED,
            reference=f"SUPPRESSION ORDRE {bordereau}", user=_user_for_request(request))
    order.delete()
    log_action(
        request.user, AuditLog.DELETE,
        description=f"Ordre {bordereau} SUPPRIMÉ ({unit_count} unités remises en stock)",
        request=request,
        target_order_barcode=bordereau,
        target_model="ShippingOrder", target_id=order_id,
    )
    return JsonResponse({"status": "ok", "message": "Ordre supprimé, unités remises en stock."})


@csrf_exempt
@require_POST
def api_order_remove_unit(request, pk):
    """Remove a unit from an order and return it to in_stock."""
    from .models import log_action, AuditLog
    data = json.loads(request.body)
    confirmation = data.get("confirmation", "")
    barcode = data.get("barcode", "")
    if confirmation != "MODIFY":
        return JsonResponse({"status": "error", "message": "Confirmation incorrecte."})
    try:
        order = ShippingOrder.objects.get(pk=pk)
        unit = ProductUnit.objects.get(barcode=barcode)
        item = OrderItem.objects.get(order=order, unit=unit)
    except (ShippingOrder.DoesNotExist, ProductUnit.DoesNotExist, OrderItem.DoesNotExist):
        return JsonResponse({"status": "error", "message": "Unité ou ordre introuvable."})
    if order.status == ShippingOrder.PAID:
        return JsonResponse({"status": "error", "message": "Impossible de modifier un ordre payé."})
    item.delete()
    unit.status = ProductUnit.IN_STOCK
    unit.save()
    StockMovement.objects.create(
        unit=unit, movement_type=StockMovement.RECEIVED,
        reference=f"MODIFICATION ORDRE {order.bordereau_barcode}", user=_user_for_request(request))
    log_action(
        request.user, AuditLog.EDIT,
        description=f"Unité {barcode} retirée de l'ordre {order.bordereau_barcode}",
        request=request,
        target_unit_barcode=barcode,
        target_order_barcode=order.bordereau_barcode,
    )
    return JsonResponse({"status": "ok", "message": f"{barcode} retiré et remis en stock."})


@csrf_exempt
@require_POST
def api_order_add_unit(request, pk):
    """Add a unit to an existing order by scanning barcode."""
    from .models import log_action, AuditLog
    data = json.loads(request.body)
    confirmation = data.get("confirmation", "")
    barcode = data.get("barcode", "").strip().upper()
    if confirmation != "MODIFY":
        return JsonResponse({"status": "error", "message": "Confirmation incorrecte."})
    try:
        order = ShippingOrder.objects.get(pk=pk)
    except ShippingOrder.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Ordre introuvable."})
    if order.status == ShippingOrder.PAID:
        return JsonResponse({"status": "error", "message": "Impossible de modifier un ordre payé."})
    try:
        unit = ProductUnit.objects.select_related("variant__product").get(barcode=barcode)
    except ProductUnit.DoesNotExist:
        return JsonResponse({"status": "error", "message": f"Unité introuvable : {barcode}"})
    if unit.status not in (ProductUnit.IN_STOCK, ProductUnit.RETURNED):
        return JsonResponse({"status": "error", "message": f"{barcode} n'est pas en stock — statut : {unit.get_status_display()}"})
    if OrderItem.objects.filter(order=order, unit=unit).exists():
        return JsonResponse({"status": "error", "message": f"{barcode} est déjà dans cet ordre."})
    OrderItem.objects.create(order=order, unit=unit, status_at_scan=ProductUnit.SHIPPED)
    unit.status = ProductUnit.SHIPPED
    unit.save()
    variant = unit.variant
    log_action(
        request.user, AuditLog.EDIT,
        description=f"Unité {barcode} ajoutée à l'ordre {order.bordereau_barcode}",
        request=request,
        target_unit_barcode=barcode,
        target_order_barcode=order.bordereau_barcode,
    )
    return JsonResponse({
        "status": "ok",
        "message": f"{variant.product.name} {variant.color_label} taille {unit.size} ajouté.",
        "unit": {
            "barcode": unit.barcode, "size": unit.size,
            "product_name": variant.product.name, "color_label": variant.color_label,
            "sell_price": str(variant.product.sell_price),
            "image_url": variant.image.url if variant.image else None,
        }
    })


# ---------------------------------------------------------------------------
# DASHBOARD & DETAIL VIEWS
# ---------------------------------------------------------------------------

@login_required(login_url="/login/")
def home_dispatcher(request):
    """Landing page at '/' — admins go to dashboard, everyone else sees the bubble menu."""
    user = request.user
    if user.is_superuser:
        return redirect("dashboard")

    # Determine role (default to office if no profile, e.g. legacy user)
    try:
        role = user.profile.role
    except Exception:
        role = "office"

    return render(request, "inventory/bubble_home.html", {
        "role": role,
        "is_messages_team": role == "messages",
    })


@login_required(login_url="/login/")
@login_required(login_url="/login/")
def bot_test_page(request):
    """Simple chat UI to test the auto-reply bot without Meta: you type as the
    customer, the bot answers using the real logic (catalogue, gender, vision)."""
    return render(request, "inventory/bot_test.html", {})


@csrf_exempt
@require_POST
def api_bot_test_reply(request):
    """Simulate a customer message and return the bot's reply. The simulated
    conversation lives in the request payload (no DB writes). Accepts optional
    base64 image (data URL) as the customer photo."""
    try:
        import json as _json
        data = _json.loads(request.body.decode("utf-8"))
        history = data.get("messages") or []   # [{from,text,images?}]
        page_id = str(data.get("page_id") or "580021675198711")
        sender_name = (data.get("sender_name") or "").strip()
        img_b64 = (data.get("image_b64") or "").strip()  # data URL or raw b64

        # Build a fake conversation object compatible with _bot_reply.
        class _FakeConv:
            pass
        conv = _FakeConv()
        conv.messages = history
        conv.page_id = page_id
        conv.sender_name = sender_name
        conv.source_ad_id = str(data.get("ad_id") or "")

        # If the tester attached an image, save it to a temp file and monkey-
        # patch the last user message so the bot sees a photo (vision reads
        # local files via local_images; we reuse the URL path by writing a
        # temp file and passing it through a fake URL list is not possible, so
        # instead we inject it via a special marker consumed below).
        tmp_path = ""
        if img_b64:
            try:
                import base64 as _b64, tempfile as _tmp
                raw = img_b64.split(",", 1)[1] if "," in img_b64 else img_b64
                blob = _b64.b64decode(raw)
                tf = _tmp.NamedTemporaryFile(delete=False, suffix=".jpg")
                tf.write(blob)
                tf.close()
                tmp_path = tf.name
                # Mark the last user message as carrying an image so the
                # transcript reflects it.
                if history and history[-1].get("from") == "user":
                    history[-1]["images"] = ["local"]
            except Exception:
                tmp_path = ""

        # Generate the reply. If a local image was provided, hand it to the bot
        # via a conversation attribute that _bot_reply reads directly.
        if tmp_path:
            conv._test_local_image = tmp_path
        try:
            reply = _bot_reply(conv)
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

        return JsonResponse({"status": "ok", "reply": reply or ""})
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)[:200]}, status=500)


def dashboard(request):
    # Determine viewing mode (which bubble was clicked).
    # Admins/superusers always see the full dashboard ("all").
    # Non-admin users without a view param get bounced back to the bubble page.
    view_mode = request.GET.get("view", "")
    if request.user.is_superuser:
        view_mode = view_mode or "all"
    else:
        try:
            role = request.user.profile.role
        except Exception:
            role = "office"
        # Messages Team can only ever see the messages view
        if role == "messages":
            view_mode = "messages"
        # Non-admin without an explicit view → back to bubble picker
        if view_mode not in ("shipping", "office", "messages"):
            return redirect("home")

    # ---- OPTIMIZED: dashboard previously made ~50 queries (one per product
    # for total_stock); now uses a single annotated query.

    # Single SQL: every product with its count of in_stock + returned units
    products_qs = Product.objects.annotate(
        _stock_count=Count(
            "variants__units",
            filter=Q(variants__units__status__in=(ProductUnit.IN_STOCK, ProductUnit.RETURNED)),
        ),
    )

    # Total products (cached from the same query — no extra round-trip)
    total_products = products_qs.count()

    # Low-stock list: filter in Python from the same data we already loaded.
    # Build a small dict per item so the template never re-queries .total_stock.
    products_for_template = list(
        products_qs.filter(archived=False, alert_disabled=False).values(
            "id", "name", "alert_threshold", "_stock_count",
        )
    )
    low_stock = [
        {
            "name": p["name"],
            "total_stock": p["_stock_count"],
            "alert_threshold": p["alert_threshold"],
        }
        for p in products_for_template
        if p["_stock_count"] <= p["alert_threshold"]
    ]

    open_order = ShippingOrder.objects.filter(status=ShippingOrder.OPEN).first()
    recent_orders = ShippingOrder.objects.order_by("-opened_at")[:10]
    now = timezone.now()
    orders_this_month = ShippingOrder.objects.filter(opened_at__year=now.year, opened_at__month=now.month).count()
    total_in_stock = ProductUnit.objects.filter(status=ProductUnit.IN_STOCK).count()
    total_shipped = ProductUnit.objects.filter(status=ProductUnit.SHIPPED).count()

    # Size alerts — skip muted/archived parents
    from .models import SizeAlert
    size_alerts = [
        sa for sa in SizeAlert.objects.select_related("variant__product").all()
        if sa.is_triggered
        and not sa.variant.product.alert_disabled
        and not sa.variant.product.archived
    ]

    # Alerts: shipped units that belong to paid orders = waiting to come back
    waiting_return = ProductUnit.objects.filter(
        status=ProductUnit.SHIPPED,
        order_items__order__status=ShippingOrder.PAID,
    ).distinct().count()
    overdue_orders = [o for o in ShippingOrder.objects.filter(status=ShippingOrder.CLOSED) if o.is_overdue]

    return render(request, "inventory/dashboard.html", {
        # NB: 'products' is no longer passed — the dashboard template doesn't
        # actually iterate every product (it only uses low_stock + size_alerts).
        "open_order": open_order, "recent_orders": recent_orders,
        "low_stock": low_stock, "total_in_stock": total_in_stock,
        "total_shipped": total_shipped, "orders_this_month": orders_this_month,
        "pending_returns": waiting_return, "total_products": total_products,
        "size_alerts": size_alerts, "overdue_orders": overdue_orders,
        "view_mode": view_mode,
    })


@login_required(login_url="/login/")
def order_detail(request, pk):
    from decimal import Decimal
    order = get_object_or_404(ShippingOrder, pk=pk)
    items = order.items.select_related("unit__variant__product", "unit__variant").order_by("-scanned_at")
    # Auto-calculate total: only non-returned items + 7 TND shipping
    ACTIVE = ("paid", "shipped", "shipped")
    calculated_total = sum(
        item.unit.variant.product.sell_price
        for item in items
        if item.display_status in ACTIVE
    ) + Decimal("7")
    return render(request, "inventory/order_detail.html", {
        "order": order,
        "items": items,
        "calculated_total": calculated_total,
    })


@login_required(login_url="/login/")
def revenue(request):
    if not request.user.is_staff:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("Accès réservé aux administrateurs.")

    from datetime import datetime, date
    RETURN_FEE = Decimal("1")

    date_from_str = request.GET.get("date_from", "")
    date_to_str   = request.GET.get("date_to", "")

    # Parse dates
    date_from = None
    date_to   = None
    try:
        if date_from_str:
            date_from = datetime.strptime(date_from_str, "%Y-%m-%d").date()
        if date_to_str:
            date_to = datetime.strptime(date_to_str, "%Y-%m-%d").date()
    except ValueError:
        pass

    # Filter paid orders by paid_at date
    paid_orders_qs = ShippingOrder.objects.filter(status=ShippingOrder.PAID)
    if date_from:
        paid_orders_qs = paid_orders_qs.filter(paid_at__date__gte=date_from)
    if date_to:
        paid_orders_qs = paid_orders_qs.filter(paid_at__date__lte=date_to)
    paid_orders_qs = paid_orders_qs.order_by("-paid_at")

    # Calculate from filtered orders
    total_sell       = Decimal("0")
    total_buy        = Decimal("0")
    total_paid_units = 0
    total_returns    = 0
    order_rows       = []

    for order in paid_orders_qs:
        items = order.items.select_related("unit__variant__product")
        paid_items   = [i for i in items if i.display_status == ProductUnit.PAID]
        return_items = [i for i in items if i.display_status == ProductUnit.RETURNED]
        sell_total = sum(i.unit.variant.product.sell_price for i in paid_items)
        buy_total  = sum(i.unit.variant.product.buy_price  for i in paid_items)
        ret_fees   = RETURN_FEE * len(return_items)
        total_sell       += sell_total
        total_buy        += buy_total
        total_paid_units += len(paid_items)
        total_returns    += len(return_items)
        order_rows.append({
            "order": order,
            "paid_count": len(paid_items),
            "return_count": len(return_items),
            "sell_total": sell_total,
            "buy_total": buy_total,
            "net": sell_total - buy_total - ret_fees,
        })

    gross_margin = total_sell - total_buy
    margin_pct   = (gross_margin / total_sell * 100) if total_sell > 0 else Decimal("0")
    return_fees  = RETURN_FEE * total_returns
    net_revenue  = gross_margin - return_fees

    return render(request, "inventory/revenue.html", {
        "total_sell": total_sell, "total_buy": total_buy,
        "total_paid_units": total_paid_units, "gross_margin": gross_margin,
        "margin_pct": margin_pct, "total_returns": total_returns,
        "return_fees": return_fees, "net_revenue": net_revenue,
        "order_rows": order_rows,
        "date_from": date_from_str, "date_to": date_to_str,
    })


@login_required(login_url="/login/")
def navex_sync_page(request):
    if not request.user.is_staff:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("Accès réservé.")
    return render(request, "inventory/navex_sync.html", {})


@csrf_exempt
@require_POST
def api_navex_sync(request):
    """Run Navex sync — check all shipped orders."""
    if not request.user.is_staff:
        return JsonResponse({"status": "error", "message": "Accès refusé."}, status=403)
    from .navex_sync import sync_shipped_orders
    results = sync_shipped_orders()
    return JsonResponse(results)


def admin_panel(request):
    if not request.user.is_staff:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("Accès réservé aux administrateurs.")
    from django.contrib.auth.models import User
    from .models import Payment
    return render(request, "inventory/admin_panel.html", {
        "product_count": Product.objects.count(),
        "variant_count": ProductVariant.objects.count(),
        "unit_count": ProductUnit.objects.count(),
        "order_count": ShippingOrder.objects.count(),
        "payment_count": Payment.objects.count(),
        "user_count": User.objects.count(),
    })


@login_required(login_url="/login/")
@csrf_exempt
@require_POST
def api_navex_status(request, pk):
    """Check Navex status for an order — read only, never modifies our data."""
    import urllib.request
    import urllib.parse

    order = get_object_or_404(ShippingOrder, pk=pk)

    try:
        data = urllib.parse.urlencode({'code': order.bordereau_barcode, 'include_prix': '1'}).encode()
        req = urllib.request.Request(
            NAVEX_API_URL,
            data=data,
            method='POST'
        )
        req.add_header('Content-Type', 'application/x-www-form-urlencoded')
        with urllib.request.urlopen(req, timeout=10) as response:
            import json as json_lib
            result = json_lib.loads(response.read().decode())
            return JsonResponse({"status": "ok", "navex": result})
    except Exception as e:
        return JsonResponse({"status": "error", "message": f"Erreur Navex : {str(e)}"})


@csrf_exempt
@require_POST
def api_confirm_payment_from_navex(request, pk):
    """Confirm payment for an order using Navex price."""
    from .models import log_action, AuditLog
    import urllib.request, urllib.parse
    data = json.loads(request.body)
    amount = Decimal(str(data.get("amount", "0")))

    try:
        order = ShippingOrder.objects.get(pk=pk)
    except ShippingOrder.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Ordre introuvable."})

    if order.status == ShippingOrder.PAID:
        return JsonResponse({"status": "error", "message": "Ordre deja paye."})

    with transaction.atomic():
        order.status = ShippingOrder.PAID
        order.paid_at = order.navex_paid_detected_at or timezone.now()
        order.amount_collected = amount
        order.save()
        for item in order.items.select_related("unit"):
            if item.unit.status == ProductUnit.SHIPPED:
                item.unit.status = ProductUnit.PAID
                item.unit.save()
                OrderItem.objects.filter(pk=item.pk).update(status_at_payment=ProductUnit.PAID)
                StockMovement.objects.create(
                    unit=item.unit, movement_type=StockMovement.PAID,
                    reference=order.bordereau_barcode, user=_user_for_request(request))
        # Mirror the collected amount onto the linked v2 Order (if any) so the
        # v2 list reflects what was actually collected. Only the collected
        # amount is synced — the computed `total` is left untouched. Also flip
        # the v2 status to PAYEE ("Payée") — but never override a return
        # (RETURNED stays final).
        v2 = getattr(order, "order", None)
        if v2 is not None:
            from .models import Order as _V2Order
            v2.amount_collected = amount
            fields = ["amount_collected", "updated_at"]
            if v2.status != _V2Order.RETURNED and v2.status != _V2Order.PAYEE:
                v2.status = _V2Order.PAYEE
                fields.append("status")
            v2.save(update_fields=fields)

    log_action(
        request.user, AuditLog.PAYMENT,
        description=f"Ordre {order.bordereau_barcode} marqué payé via Navex — {amount} TND",
        request=request,
        target_order_barcode=order.bordereau_barcode,
        target_model="ShippingOrder", target_id=order.id,
    )

    # If this ShippingOrder is linked to a v2 Order that came from Shopify,
    # mirror the PAID status back to Shopify (creates a successful transaction
    # so the merchant dashboard shows the order as Paid instead of Pending).
    try:
        v2 = order.order
        if v2 and v2.notes:
            shopify_id = _extract_shopify_order_id_from_notes(v2.notes)
            if shopify_id:
                ok_sh, sh_resp = _shopify_mark_paid(shopify_id, amount, "TND")
                if ok_sh:
                    log_action(
                        request.user, AuditLog.PAYMENT,
                        description=f"Shopify : commande {shopify_id} marquée payée ({amount} TND, Order v2 #{v2.id})",
                        request=request,
                        target_model="Order", target_id=v2.id,
                    )
                else:
                    err_msg = sh_resp.get("_error") if isinstance(sh_resp, dict) else str(sh_resp)
                    log_action(
                        request.user, AuditLog.PAYMENT,
                        description=f"Shopify : ÉCHEC mark paid pour {shopify_id} (Order v2 #{v2.id}) — {err_msg}",
                        request=request,
                        target_model="Order", target_id=v2.id,
                    )
    except Exception as _e:
        # Never let Shopify sync break our local payment confirmation
        pass

    return JsonResponse({"status": "ok", "message": f"Ordre {order.bordereau_barcode} marque comme paye — {amount} TND."})


@csrf_exempt
def api_navex_en_attente(request):
    """En-attente list for scan expédition. HYBRID source:
      1) Primary: our own v2 Orders that are confirmée, have a bordereau, and
         are not yet shipped — with products from the REAL OrderLines (accurate).
      2) Fallback: any barcode Navex lists as en-attente that has NO order in our
         system (e.g. created directly in Navex) — matched from the Navex
         designation text so it isn't hidden.
    A barcode that already has a ShippingOrder (scanned/expédié) is excluded."""
    import urllib.request, urllib.parse
    from .models import Order
    from .scan_service import _matched_products_from_order, _get_matched_products
    from . import scan_service

    try:
        # Barcodes already scanned at expédition (any ShippingOrder) → exclude.
        scanned = set(ShippingOrder.objects
                      .exclude(bordereau_barcode="")
                      .values_list("bordereau_barcode", flat=True))

        # --- (1) v2 orders: confirmée, pushed, not yet shipped ---
        shipped_or_beyond = [
            Order.EN_COURS, Order.AU_MAGASIN, Order.RETURNING, Order.RETURNED,
            Order.LIVREE, Order.PAYEE, Order.ANNULEE,
        ]
        v2_orders = (Order.objects
                     .filter(status=Order.CONFIRMEE)
                     .exclude(bordereau_barcode="")
                     .exclude(status__in=shipped_or_beyond)
                     .select_related("customer", "sales_page", "region")
                     .prefetch_related("lines__product", "lines__variant")
                     .order_by("-created_at"))

        result = []
        seen_barcodes = set()
        for o in v2_orders:
            bc = o.bordereau_barcode
            if not bc or bc in scanned or bc in seen_barcodes:
                continue
            seen_barcodes.add(bc)
            matched = _matched_products_from_order(o)
            result.append({
                "code_barre": bc,
                "designation": o.article_summary if hasattr(o, "article_summary") else "",
                "prix": str(o.total),
                "nom": o.display_name,
                "tel": o.customer.phone if o.customer else "",
                "ville": o.ville or "",
                "page": o.sales_page.name if o.sales_page else "",
                "order_id": o.id,
                "matched_products": matched,
                "recognized": len(matched) > 0,
            })

        # --- (2) Navex-only fallback: barcodes Navex has en attente that we
        # don't know about (no order in our system). Keep them visible. ---
        try:
            data = urllib.parse.urlencode({"getattente": "1"}).encode()
            req = urllib.request.Request(NAVEX_API_URL, data=data, method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            with urllib.request.urlopen(req, timeout=15) as resp:
                import json as json_lib
                navex_data = json_lib.loads(resp.read().decode())
            our_order_barcodes = set(
                Order.objects.exclude(bordereau_barcode="")
                .values_list("bordereau_barcode", flat=True)
            )
            for colis in navex_data.get("colis", []):
                bc = colis.get("code_barre", "")
                if not bc or bc in scanned or bc in seen_barcodes:
                    continue
                # Only add if we have NO order for it (else v2 already covered it
                # or it's intentionally not pending on our side).
                if bc in our_order_barcodes:
                    continue
                seen_barcodes.add(bc)
                designation = colis.get("designation", "")
                matched = _get_matched_products(designation)
                result.append({
                    "code_barre": bc,
                    "designation": designation,
                    "prix": colis.get("prix", ""),
                    "nom": colis.get("nom", "") or colis.get("client_nom", "") or colis.get("name", ""),
                    "tel": colis.get("tel", "") or colis.get("phone", "") or colis.get("telephone", ""),
                    "ville": colis.get("ville", "") or colis.get("city", ""),
                    "matched_products": matched,
                    "recognized": len(matched) > 0,
                    "navex_only": True,
                })
        except Exception:
            # If Navex is unreachable, still return the v2 list.
            pass

        scan_service.navexMap_cache = {c["code_barre"]: c for c in result}
        return JsonResponse({"status": "ok", "total": len(result), "colis": result})

    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)})

    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)})


@login_required(login_url="/login/")
def a_verifier(request):
    if not request.user.is_staff:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden()

    now = timezone.now()
    closed_orders = ShippingOrder.objects.filter(
        status__in=(ShippingOrder.CLOSED, ShippingOrder.PARTIAL_RETURNED)
    ).order_by("closed_at")

    orders_to_verify = []
    for order in closed_orders:
        if not order.closed_at:
            continue
        closed_weekday = order.closed_at.weekday()
        limit_hours = 58 if closed_weekday == 5 else 34
        hours_since = (now - order.closed_at).total_seconds() / 3600
        if hours_since >= limit_hours:
            verification, _ = OrderVerification.objects.get_or_create(order=order)
            # Reset treated if it was treated on a previous day
            if verification.treated and verification.treated_at:
                if verification.treated_at.date() < now.date():
                    verification.treated = False
                    verification.treated_at = None
                    verification.save(update_fields=["treated", "treated_at"])
            orders_to_verify.append({
                "order": order,
                "verification": verification,
                "hours_since": round(hours_since, 1),
                "limit_hours": limit_hours,
            })

    # Fetch Navex status for all orders at once
    navex_map = {}
    try:
        import urllib.request, urllib.parse
        codes = [o["order"].bordereau_barcode for o in orders_to_verify]
        if codes:
            codes_string = ", ".join(codes)
            data = urllib.parse.urlencode({"codes": codes_string, "include_prix": "1"}).encode()
            req = urllib.request.Request(
                NAVEX_API_URL,
                data=data, method="POST"
            )
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            with urllib.request.urlopen(req, timeout=15) as resp:
                import json as json_lib
                navex_data = json_lib.loads(resp.read().decode())
            for result in navex_data.get("results", []):
                if result.get("status") == 1:
                    navex_map[result["code"]] = result.get("etat", "")
    except Exception:
        pass

    # Filter out orders that Navex says are delivered
    DELIVERED_STATES = ("Livrer", "Livrer Paye", "Livré", "Livré Payé", "Livree", "Retourné", "Retour recu", "Rtn client/agence")
    filtered_orders = []
    for o in orders_to_verify:
        etat = navex_map.get(o["order"].bordereau_barcode, "Inconnu")
        o["navex_etat"] = etat
        if etat not in DELIVERED_STATES:
            filtered_orders.append(o)
    orders_to_verify = filtered_orders

    treated_count = sum(1 for o in orders_to_verify if o["verification"].treated)
    untreated_count = len(orders_to_verify) - treated_count

    # Incomplete-bundle detection (retroactive): scan recently-closed orders and
    # flag any where the number of scanned units differs from the number of
    # pieces the order's offers expect (e.g. an Ensemble with 2 pieces but only
    # 1 scanned). Independent of the 34h/58h delay — these need attention now.
    incomplete_bundles = []
    try:
        from .scan_service import _matched_products_from_order as _mpfo
        recent_closed = (ShippingOrder.objects
                         .filter(status__in=(ShippingOrder.CLOSED,
                                             ShippingOrder.PARTIAL_RETURNED))
                         .exclude(order__isnull=True)
                         .select_related("order")
                         .order_by("-closed_at")[:400])
        for so in recent_closed:
            v2 = so.order
            if v2 is None:
                continue
            expected = len(_mpfo(v2))
            scanned = so.items.count()
            if expected and scanned != expected:
                incomplete_bundles.append({
                    "bordereau": so.bordereau_barcode,
                    "order_id": v2.id,
                    "scanned": scanned,
                    "expected": expected,
                    "client": v2.display_name,
                    "closed_at": so.closed_at,
                    "status": v2.status,
                })
    except Exception:
        incomplete_bundles = []

    return render(request, "inventory/a_verifier.html", {
        "orders": orders_to_verify,
        "treated_count": treated_count,
        "untreated_count": untreated_count,
        "incomplete_bundles": incomplete_bundles,
    })


@csrf_exempt
@require_POST
def api_mark_treated(request, pk):
    try:
        order = ShippingOrder.objects.get(pk=pk)
        verification, _ = OrderVerification.objects.get_or_create(order=order)
        verification.treated = not verification.treated
        verification.treated_at = timezone.now() if verification.treated else None
        verification.save()
        return JsonResponse({"status": "ok", "treated": verification.treated})
    except ShippingOrder.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Ordre introuvable."})


@csrf_exempt
@require_POST
def api_save_navex_info(request, pk):
    """Save Navex info to an order — called from JS after scan."""
    from .models import log_action, AuditLog
    try:
        data = json.loads(request.body)
        ShippingOrder.objects.filter(pk=pk).update(
            amount_collected=data.get("prix") or None,
            client_name=data.get("nom", ""),
            client_phone=data.get("tel", ""),
            client_address=data.get("adresse", ""),
            client_ville=data.get("ville", ""),
            navex_designation=data.get("designation", ""),
        )
        try:
            order = ShippingOrder.objects.get(pk=pk)
            log_action(
                request.user, AuditLog.EDIT,
                description=f"Infos Navex enregistrées pour ordre {order.bordereau_barcode} (client: {data.get('nom','')[:60]})",
                request=request,
                target_order_barcode=order.bordereau_barcode,
                target_model="ShippingOrder", target_id=order.id,
            )
        except ShippingOrder.DoesNotExist:
            pass
        return JsonResponse({"status": "ok"})
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)})


def api_check_duplicate_client(request):
    """Check whether a phone number has other ShippingOrders still in the
    delivery cycle (OPEN or CLOSED — i.e. not yet PAID/RETURNED).

    Used to warn the warehouse team when they're about to ship a SECOND
    parcel to a client who already has an in-flight first parcel — common
    pattern of duplicate orders across multiple sales pages.

    GET ?phone=12345678&exclude_id=NNN
    """
    phone_raw = (request.GET.get("phone") or "").strip()
    exclude_id = request.GET.get("exclude_id") or ""

    if not phone_raw:
        return JsonResponse({"status": "ok", "duplicates": []})

    # Normalize: keep digits only (handles "+216 12-345-678" → "21612345678")
    import re as _re
    digits = _re.sub(r"\D", "", phone_raw)
    if len(digits) < 6:
        return JsonResponse({"status": "ok", "duplicates": []})

    # Match by suffix to tolerate +216 prefix variations.
    # We only return non-finalized orders (still in delivery cycle).
    suffix = digits[-8:]  # last 8 digits = TN phone number
    qs = ShippingOrder.objects.filter(client_phone__icontains=suffix)
    if exclude_id:
        try:
            qs = qs.exclude(pk=int(exclude_id))
        except Exception:
            pass
    # Only orders still in delivery cycle (not paid, not returned).
    # ShippingOrder has no "cancelled" status — cancellation lives on the v2
    # Order (ANNULEE), so we exclude the finalized ShippingOrder states only.
    qs = qs.exclude(status__in=(
        ShippingOrder.PAID,
        ShippingOrder.PARTIAL_PAID,
        ShippingOrder.RETURNED,
        ShippingOrder.PARTIAL_RETURNED,
    ))
    qs = qs.order_by("-created_at")[:5]

    duplicates = []
    for o in qs:
        duplicates.append({
            "id": o.id,
            "bordereau_barcode": o.bordereau_barcode,
            "client_name": o.client_name,
            "status": o.get_status_display(),
            "created_at": o.created_at.strftime("%d/%m/%Y %H:%M"),
            "designation": o.navex_designation[:120] if o.navex_designation else "",
        })
    return JsonResponse({"status": "ok", "duplicates": duplicates})


def api_get_order_amount(request, pk):
    """Get amount for an order — from DB or fetch from Navex."""
    try:
        order = ShippingOrder.objects.get(pk=pk)
        # If we have it saved, return it
        if order.amount_collected:
            return JsonResponse({
                "status": "ok",
                "amount_collected": str(order.amount_collected),
            })
        # Otherwise fetch from Navex single status API
        try:
            import urllib.request, urllib.parse
            data = urllib.parse.urlencode({
                "code": order.bordereau_barcode,
                "include_prix": "1"
            }).encode()
            req = urllib.request.Request(
                NAVEX_API_URL,
                data=data, method="POST"
            )
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            with urllib.request.urlopen(req, timeout=5) as resp:
                import json as json_lib
                navex_data = json_lib.loads(resp.read().decode())
            prix = navex_data.get("prix")
            if prix:
                # Save it
                ShippingOrder.objects.filter(pk=pk).update(amount_collected=prix)
                return JsonResponse({"status": "ok", "amount_collected": str(prix)})
        except Exception:
            pass
        return JsonResponse({"status": "ok", "amount_collected": None})
    except ShippingOrder.DoesNotExist:
        return JsonResponse({"status": "error"})


@csrf_exempt
@require_POST  
def api_create_return_order(request):
    """Create a new return order from an unknown barcode scanned in retour section."""
    from .models import log_action, AuditLog
    data = json.loads(request.body)
    barcode = data.get("barcode", "").strip().upper()
    if not barcode:
        return JsonResponse({"status": "error", "message": "Barcode manquant."})
    
    # Check it doesn't already exist
    if ShippingOrder.objects.filter(bordereau_barcode=barcode).exists():
        return JsonResponse({"status": "error", "message": "Ce bordereau existe déjà."})
    
    order = ShippingOrder.objects.create(
        bordereau_barcode=barcode,
        status=ShippingOrder.OPEN,
        notes="Ordre retour",
    )
    log_action(
        request.user, AuditLog.CREATE,
        description=f"Ordre retour créé : {barcode}",
        request=request,
        target_order_barcode=barcode,
        target_model="ShippingOrder", target_id=order.id,
    )
    return JsonResponse({
        "status": "ok",
        "order": {"id": order.id, "bordereau_barcode": order.bordereau_barcode},
        "message": f"Ordre retour {barcode} créé.",
    })


@csrf_exempt
@require_POST
def api_return_unit_to_order(request, pk):
    """Move a unit from its original order to a return order."""
    from .models import log_action, AuditLog
    data = json.loads(request.body)
    barcode = data.get("barcode", "").strip().upper()
    
    try:
        return_order = ShippingOrder.objects.get(pk=pk)
    except ShippingOrder.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Ordre retour introuvable."})
    
    try:
        unit = ProductUnit.objects.select_related("variant__product").get(barcode=barcode)
    except ProductUnit.DoesNotExist:
        return JsonResponse({"status": "error", "message": f"Unité {barcode} introuvable."})
    
    if unit.status not in (ProductUnit.SHIPPED, ProductUnit.PAID,
                           ProductUnit.EARLY_RETURN, ProductUnit.AT_DEPOT):
        return JsonResponse({"status": "error", "message": f"Cette unité ne peut pas être retournée (statut: {unit.status})."})
    
    # Find original order
    original_item = unit.order_items.select_related("order").order_by("-scanned_at").first()
    original_order = original_item.order if original_item else None
    
    with transaction.atomic():
        # Create item in return order
        OrderItem.objects.get_or_create(
            order=return_order,
            unit=unit,
            defaults={"status_at_scan": unit.status}
        )
        
        # Mark unit as returned
        unit.status = ProductUnit.RETURNED
        unit.save()
        StockMovement.objects.create(
            unit=unit, movement_type=StockMovement.RETURNED,
            reference=return_order.bordereau_barcode, user=_user_for_request(request))
        
        # Update original order status and amount
        if original_order:
            if original_item:
                original_item.status_at_payment = ProductUnit.RETURNED
                original_item.save(update_fields=["status_at_payment"])
            
            # Deduct from original order amount if it was paid at full price
            if original_order.amount_collected:
                unit_price = unit.variant.product.sell_price
                new_amount = max(original_order.amount_collected - unit_price, Decimal("0"))
                original_order.amount_collected = new_amount
            
            _update_order_return_status(original_order)
        
        # Update return order status
        return_items = return_order.items.select_related("unit").all()
        return_statuses = [i.unit.status for i in return_items]
        if return_statuses and all(s == ProductUnit.RETURNED for s in return_statuses):
            return_order.status = ShippingOrder.RETURNED
        else:
            return_order.status = ShippingOrder.PARTIAL_RETURNED
        return_order.save(update_fields=["status"])
    
    log_action(
        request.user, AuditLog.SCAN_RETURN,
        description=f"Unité {barcode} retournée vers ordre retour {return_order.bordereau_barcode}" + (f" (depuis {original_order.bordereau_barcode})" if original_order else ""),
        request=request,
        target_unit_barcode=barcode,
        target_order_barcode=return_order.bordereau_barcode,
    )

    return JsonResponse({
        "status": "ok",
        "message": f"{unit.variant.product.name} {unit.variant.color_label} — {unit.size} retourné.",
        "unit": {
            "barcode": unit.barcode,
            "product_name": unit.variant.product.name,
            "color_label": unit.variant.color_label,
            "size": unit.size,
            "image_url": unit.variant.image.url if unit.variant.image else None,
        },
        "original_order": original_order.bordereau_barcode if original_order else None,
    })


@csrf_exempt
@require_POST
def api_set_size_alert(request, variant_pk, size):
    """Set alert threshold for a variant+size combination."""
    if not request.user.is_superuser:
        return JsonResponse({"status": "error", "message": "Admin uniquement."})
    data = json.loads(request.body)
    threshold = int(data.get("threshold", 3))
    alert, created = SizeAlert.objects.get_or_create(
        variant_id=variant_pk, size=size,
        defaults={"threshold": threshold}
    )
    if not created:
        alert.threshold = threshold
        alert.save()
    return JsonResponse({"status": "ok", "threshold": alert.threshold})

@csrf_exempt
def api_get_size_alert(request, variant_pk, size):
    """Get current alert threshold for a variant+size."""
    try:
        alert = SizeAlert.objects.get(variant_id=variant_pk, size=size)
        return JsonResponse({"status": "ok", "threshold": alert.threshold})
    except SizeAlert.DoesNotExist:
        return JsonResponse({"status": "ok", "threshold": None})


@csrf_exempt
@require_POST
def api_log_scan_session(request):
    """Save a closed order to the daily scan session log.
    Idempotent — same bordereau scanned twice today updates the existing row
    instead of creating a duplicate."""
    data = json.loads(request.body)
    today = get_scan_session_date()  # rolls Saturday session over Sunday
    bc = (data.get("bordereau") or "").strip()
    if not bc:
        return JsonResponse({"status": "error", "message": "Bordereau vide."}, status=400)
    ScanSessionLog.objects.update_or_create(
        session_date=today,
        bordereau_barcode=bc,
        defaults={
            "designation": data.get("designation", ""),
            "unit_count": data.get("unit_count", 0),
            "is_correct": data.get("is_correct", True),
            "reason": data.get("reason", ""),
        },
    )
    return JsonResponse({"status": "ok"})


@login_required(login_url="/login/")
def api_get_scan_session(request):
    """Get today's scan session log — dedupes by bordereau (latest wins).
    Also resolves order_id for each bordereau so the UI can link to detail pages.
    """
    today = get_scan_session_date()  # rolls Saturday session over Sunday
    logs = ScanSessionLog.objects.filter(session_date=today).order_by("-scanned_at")
    seen = set()
    deduped = []
    for log in logs:
        if log.bordereau_barcode in seen:
            continue
        seen.add(log.bordereau_barcode)
        deduped.append(log)

    # Resolve order_id per bordereau in ONE query (avoid N+1)
    bordereaux = [l.bordereau_barcode for l in deduped]
    order_ids = dict(
        ShippingOrder.objects.filter(bordereau_barcode__in=bordereaux)
        .values_list("bordereau_barcode", "id")
    )

    def serialize(l, is_correct):
        d = {
            "bc": l.bordereau_barcode,
            "designation": l.designation,
            "units": l.unit_count,
            "time": l.scanned_at.strftime("%H:%M"),
            "order_id": order_ids.get(l.bordereau_barcode),
        }
        if not is_correct:
            d["reason"] = l.reason
        return d

    return JsonResponse({
        "status": "ok",
        "correct": [serialize(l, True) for l in deduped if l.is_correct],
        "wrong": [serialize(l, False) for l in deduped if not l.is_correct],
    })


@csrf_exempt
@require_POST
@login_required(login_url="/login/")
def api_clear_scan_session_today(request):
    """Delete all ScanSessionLog rows for today (the "Vider" button).

    Does NOT touch ShippingOrders, ProductUnits, or any real shipping data —
    only clears the display log so the warehouse team starts fresh after a
    big pickup or a holiday batch.

    Requires staff permission to avoid accidental clicks.
    """
    if not request.user.is_staff:
        return JsonResponse({"status": "error", "message": "Permission refusée."}, status=403)

    today = get_scan_session_date()
    qs = ScanSessionLog.objects.filter(session_date=today)
    deleted_count = qs.count()
    qs.delete()

    log_action(
        request.user, AuditLog.OTHER,
        description=f"Session scan : vidé manuellement ({deleted_count} entrée(s) supprimée(s) pour {today})",
        request=request,
    )

    return JsonResponse({"status": "ok", "deleted": deleted_count})


@login_required(login_url="/login/")
def api_recheck_session(request):
    """Full reconciliation: walk every order closed today, fetch Navex info,
    rebuild the ScanSessionLog rows. Creates missing rows, updates existing
    ones — re-derives is_correct from Navex designation vs scanned products.
    """
    import urllib.request, urllib.parse
    today = get_scan_session_date()  # rolls Saturday session over Sunday

    # All orders closed today (any status that comes after CLOSED counts)
    # The session groups Saturday + Sunday together under the Saturday date
    # (see get_scan_session_date). So when today (the session date) is a
    # Saturday, also include the following Sunday's closed orders — otherwise
    # Sunday scans (which belong to this session) are missed by the recheck.
    from datetime import timedelta as _tdlt
    _session_dates = [today]
    if today.weekday() == 5:  # Saturday
        _session_dates.append(today + _tdlt(days=1))  # Sunday
    todays_orders = list(
        ShippingOrder.objects
        .filter(closed_at__date__in=_session_dates, status__in=ShippingOrder.CLOSED_STATUSES)
        .prefetch_related("items__unit__variant__product")
    )

    if not todays_orders:
        return JsonResponse({"status": "ok", "checked": 0, "created": 0, "updated": 0})

    barcodes = [o.bordereau_barcode for o in todays_orders]

    # Bulk-fetch Navex status + prix
    navex_etat = {}
    navex_prix = {}
    try:
        codes_string = ", ".join(barcodes)
        data = urllib.parse.urlencode({"codes": codes_string, "include_prix": "1"}).encode()
        req = urllib.request.Request(NAVEX_API_URL, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=15) as resp:
            navex_data = json.loads(resp.read().decode())
        for result in navex_data.get("results", []):
            if result.get("status") == 1:
                navex_etat[result["code"]] = result.get("etat", "")
                navex_prix[result["code"]] = result.get("prix")
    except Exception:
        return JsonResponse({"status": "error", "message": "Navex indisponible."})

    # Bulk-fetch designations from getattente (those in en-attente still have it)
    navex_designation = {}
    try:
        data = urllib.parse.urlencode({"getattente": "1"}).encode()
        req = urllib.request.Request(NAVEX_API_URL, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=15) as resp:
            attente = json.loads(resp.read().decode())
        for colis in attente.get("colis", []):
            navex_designation[colis.get("code_barre", "")] = colis.get("designation", "")
    except Exception:
        # Designation lookup is best-effort — skip if Navex burps
        pass

    all_products = list(Product.objects.all())
    created = 0
    updated = 0

    for order in todays_orders:
        bc = order.bordereau_barcode
        etat = navex_etat.get(bc, "INTROUVABLE")
        prix_navex = navex_prix.get(bc)
        # Prefer designation we already saved on the order, fall back to Navex en-attente
        designation = order.navex_designation or navex_designation.get(bc, "")

        scanned_names = list(
            order.items.values_list("unit__variant__product__name", flat=True)
        )
        scanned_lower = [n.lower() for n in scanned_names if n]

        reasons = []

        if etat == "INTROUVABLE":
            reasons.append("Introuvable sur Navex — possible annulation")
        elif etat in ("Annulé", "Annule", "Annulée"):
            reasons.append("Annulé sur Navex")

        # Price mismatch (only meaningful when we have both sides)
        if prix_navex and order.amount_collected:
            try:
                diff = abs(float(prix_navex) - float(order.amount_collected))
                if diff > 0.1:
                    reasons.append(
                        f"Prix différent — Navex: {prix_navex} TND / Notre: {order.amount_collected} TND"
                    )
            except (TypeError, ValueError):
                pass

        # Designation vs scanned products: compare with QUANTITIES.
        # Navex sometimes lists the same product multiple times (one per unit
        # ordered). We count how many of each product are expected vs scanned.
        if designation:
            items_in_desig = [part.strip() for part in designation.split(",")]
            if items_in_desig and "|" in items_in_desig[0]:
                items_in_desig[0] = items_in_desig[0].split("|", 1)[1].strip()

            # Count expected: each item line in the designation is one unit
            from collections import Counter
            expected_counter = Counter()
            for item in items_in_desig:
                item_lower = item.lower()
                # Use longest product name first to handle "Polo Ling Hiver" vs "Polo Ling"
                sorted_products = sorted(all_products, key=lambda p: len(p.name), reverse=True)
                for product in sorted_products:
                    if product.name.lower() in item_lower:
                        # Use the parent's name if this is a V2/V3 (so V2 counts as same SKU)
                        canonical_name = (
                            product.parent_product.name.lower()
                            if product.parent_product
                            else product.name.lower()
                        )
                        expected_counter[canonical_name] += 1
                        break

            # Count scanned: walk units we actually scanned. Use canonical name too
            scanned_counter = Counter()
            for n in scanned_names:
                if not n:
                    continue
                # Look up the product to find its parent if any
                canonical = n.lower()
                for p in all_products:
                    if p.name.lower() == n.lower() and p.parent_product:
                        canonical = p.parent_product.name.lower()
                        break
                scanned_counter[canonical] += 1

            # Find missing (expected but not enough scanned) and excess (scanned more than expected)
            missing_parts = []
            excess_parts = []
            for product_name, exp_qty in expected_counter.items():
                scan_qty = scanned_counter.get(product_name, 0)
                if scan_qty < exp_qty:
                    missing_parts.append(f"{product_name} (×{exp_qty - scan_qty})")
            for product_name, scan_qty in scanned_counter.items():
                exp_qty = expected_counter.get(product_name, 0)
                if scan_qty > exp_qty:
                    excess_parts.append(f"{product_name} (+{scan_qty - exp_qty})")

            if missing_parts:
                reasons.append(f"Produits manquants: {', '.join(missing_parts)}")
            if excess_parts:
                reasons.append(f"Produits en trop: {', '.join(excess_parts)}")

        # Offer-based bundle completeness: if this v1 order is linked to a v2
        # Order, expand its offers into component pieces (Ensemble -> Pants +
        # Shirt) and compare the piece count to the scanned unit count. Catches
        # incomplete bundles even when the Navex designation didn't list every
        # piece (the common case for offers).
        try:
            if order.order_id:
                from .scan_service import _matched_products_from_order as _mpfo
                _v2 = order.order
                if _v2 is not None:
                    _expected_pieces = len(_mpfo(_v2))
                    _scanned_units = order.items.count()
                    if _expected_pieces and _scanned_units != _expected_pieces:
                        _msg = (f"Ensemble incomplet : {_scanned_units} scannée(s) / "
                                f"{_expected_pieces} attendue(s)")
                        if _msg not in reasons:
                            reasons.append(_msg)
        except Exception:
            pass

        is_correct = not reasons
        reason_text = " | ".join(reasons)

        # Upsert today's log row for this barcode
        log = ScanSessionLog.objects.filter(
            session_date=today, bordereau_barcode=bc
        ).first()
        if log:
            log.designation = designation
            log.unit_count = len(scanned_names)
            log.is_correct = is_correct
            log.reason = reason_text
            log.save(update_fields=["designation", "unit_count", "is_correct", "reason"])
            updated += 1
        else:
            ScanSessionLog.objects.create(
                bordereau_barcode=bc,
                designation=designation,
                unit_count=len(scanned_names),
                is_correct=is_correct,
                reason=reason_text,
                session_date=today,
            )
            created += 1

    # Return counts that match what /api/scan-session/today/ returns
    # (deduped by bordereau, only logs for today). This ensures the Recheck
    # button shows the same number as CORRECTS in the UI.
    today_logs = ScanSessionLog.objects.filter(session_date=today).order_by("-scanned_at")
    seen = set()
    correct_count = 0
    wrong_count = 0
    for log in today_logs:
        if log.bordereau_barcode in seen:
            continue
        seen.add(log.bordereau_barcode)
        if log.is_correct:
            correct_count += 1
        else:
            wrong_count += 1

    return JsonResponse({
        "status": "ok",
        "checked": correct_count,  # what the button displays — matches CORRECTS
        "correct": correct_count,
        "wrong": wrong_count,
        "created": created,
        "updated": updated,
    })


import resend as resend_client

import socket as _socket

# Configure Resend client from env (never hard-code the key)
resend_client.api_key = os.environ.get("RESEND_API_KEY", "")

# Resend free plan only delivers to verified addresses.
# Until a domain is verified, send everything to this single recipient.
EMAIL_RECIPIENTS = ["ibrahimrahmen0@gmail.com"]
EMAIL_FROM = "Stock Manager <onboarding@resend.dev>"


def _send_email(subject, body):
    """Send a plain-text email via Resend. Returns True on success, False otherwise."""
    if not resend_client.api_key:
        # No key configured (e.g. local dev) — silently no-op instead of crashing.
        return False
    try:
        resend_client.Emails.send({
            "from": EMAIL_FROM,
            "to": EMAIL_RECIPIENTS,
            "subject": subject,
            "text": body,
        })
        return True
    except Exception:
        # Match previous fail_silently behaviour
        return False

def _build_low_stock_items():
    """One alert per SKU family (parent product). For each family we compute,
    per size, the COMBINED stock + sell-rate across parent + V2/V3, and list
    only the sizes that are running low. Returns list of:
        {name, code, image, low_sizes: [{size, stock, info}], total_stock}
    Families with no low size are skipped."""
    from .models import compute_family_size_forecast
    from django.db.models import Q

    # Find all family roots among active products.
    products = Product.objects.filter(alert_disabled=False, archived=False)
    roots = {}
    for p in products:
        root = p.parent_product or p
        roots.setdefault(root.id, root)

    items = []
    for root in roots.values():
        fam = Product.objects.filter(Q(id=root.id) | Q(parent_product=root))
        fam_ids = list(fam.values_list("id", flat=True))

        # all sizes present anywhere in the family
        sizes = set(
            ProductUnit.objects.filter(variant__product_id__in=fam_ids)
            .values_list("size", flat=True).distinct()
        )
        low_sizes = []
        total_stock = 0
        for size in sorted(s for s in sizes if s):
            f = compute_family_size_forecast(fam_ids, size)
            total_stock += f["current_stock"]
            if f["is_triggered"]:
                if f["days_of_cover"] is not None:
                    info = f"~{f['days_of_cover']}j restants à {f['daily_rate']}/j"
                else:
                    info = "rupture" if f["current_stock"] == 0 else "stock bas"
                low_sizes.append({
                    "size": size, "stock": f["current_stock"], "info": info,
                })

        if not low_sizes:
            continue

        # photo: first family variant with an image
        img = None
        for fam_p in fam.prefetch_related("variants"):
            for v in fam_p.variants.all():
                if v.image:
                    img = v.image; break
            if img:
                break

        items.append({
            "name": root.name, "code": root.code,
            "image": img, "low_sizes": low_sizes, "total_stock": total_stock,
        })
    return items


def _send_telegram_photo(photo_path, caption, chat_id=None, token=None):
    """Send a local photo file with caption via Telegram. Best-effort; returns
    True/False. Sends to all configured chat ids."""
    import urllib.request, mimetypes, uuid, os as _os
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_ids = (chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")).split(",")
    if not token or not photo_path or not _os.path.exists(photo_path):
        return False
    sent_any = False
    try:
        with open(photo_path, "rb") as fh:
            file_bytes = fh.read()
    except Exception:
        return False
    fname = _os.path.basename(photo_path) or "photo.jpg"
    ctype = mimetypes.guess_type(fname)[0] or "image/jpeg"
    for cid in chat_ids:
        cid = cid.strip()
        if not cid:
            continue
        try:
            boundary = "----tg" + uuid.uuid4().hex
            body = b""
            # text fields
            for field, val in (("chat_id", cid), ("caption", caption)):
                body += ("--" + boundary + "\r\n").encode()
                body += ('Content-Disposition: form-data; name="%s"\r\n\r\n' % field).encode()
                body += (val + "\r\n").encode("utf-8")
            # photo file
            body += ("--" + boundary + "\r\n").encode()
            body += ('Content-Disposition: form-data; name="photo"; filename="%s"\r\n' % fname).encode()
            body += ("Content-Type: %s\r\n\r\n" % ctype).encode()
            body += file_bytes + b"\r\n"
            body += ("--" + boundary + "--\r\n").encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendPhoto",
                data=body,
                headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
            sent_any = True
        except Exception:
            continue
    return sent_any


def _send_telegram(message, chat_id=None, token=None):
    """Send a message via Telegram Bot API. Reads token + chat_id from env
    (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID) unless passed explicitly. Best-effort:
    never raises, returns True/False. Supports multiple chat ids comma-separated."""
    import urllib.parse, urllib.request, json as _json
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_ids = (chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")).split(",")
    if not token:
        return False
    sent_any = False
    for cid in chat_ids:
        cid = cid.strip()
        if not cid:
            continue
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            data = urllib.parse.urlencode({
                "chat_id": cid, "text": message, "disable_web_page_preview": "true",
            }).encode()
            req = urllib.request.Request(url, data=data)
            with urllib.request.urlopen(req, timeout=20) as resp:
                resp.read()
            sent_any = True
        except Exception:
            continue
    return sent_any


def _send_whatsapp(message, phone=None, apikey=None):
    """Send a WhatsApp message via CallMeBot. Reads phone + apikey from env
    (CALLMEBOT_PHONE, CALLMEBOT_APIKEY) unless passed explicitly. Best-effort:
    never raises, returns True/False. Supports multiple recipients by comma in
    CALLMEBOT_PHONE / CALLMEBOT_APIKEY (matched by position)."""
    import urllib.parse, urllib.request
    phones = (phone or os.environ.get("CALLMEBOT_PHONE", "")).split(",")
    keys = (apikey or os.environ.get("CALLMEBOT_APIKEY", "")).split(",")
    sent_any = False
    for i, ph in enumerate(phones):
        ph = ph.strip()
        if not ph:
            continue
        key = keys[i].strip() if i < len(keys) else (keys[0].strip() if keys else "")
        if not key:
            continue
        try:
            url = (
                "https://api.callmebot.com/whatsapp.php?"
                + urllib.parse.urlencode({"phone": ph, "text": message, "apikey": key})
            )
            with urllib.request.urlopen(url, timeout=20) as resp:
                resp.read()
            sent_any = True
        except Exception:
            continue
    return sent_any


def _send_low_stock_whatsapp():
    """Send the low-stock report to Telegram: one message per family (parent
    product) listing only the sizes that are running low (combined across all
    versions), with the parent photo. (Name kept for existing cron call sites.)"""
    items = _build_low_stock_items()
    if not items:
        return False

    header = (
        f"\U0001F6A8 STOCK BAS — {len(items)} produit(s) à réapprovisionner\n"
        f"{timezone.now().strftime('%d/%m/%Y %H:%M')}"
    )
    _send_telegram(header)

    text_only = []
    for item in items:
        size_lines = "\n".join(
            f"   • Taille {s['size']} : {s['stock']} u  ({s['info']})"
            for s in item["low_sizes"]
        )
        caption = (
            f"\U0001F4E6 {item['name']}  ({item['code']})\n"
            f"Stock total (toutes versions) : {item['total_stock']} u\n"
            f"Tailles en rupture/bas :\n{size_lines}"
        )
        img = item.get("image")
        sent_photo = False
        if img:
            try:
                sent_photo = _send_telegram_photo(img.path, caption)
            except Exception:
                sent_photo = False
        if not sent_photo:
            text_only.append(caption)

    if text_only:
        _send_telegram("\n\n".join(text_only))
    return True


def _send_low_stock_email():
    """Send low stock report email — predictive (size will run out in <10 days).
    Skips products with alert_disabled=True or archived=True."""
    from .models import ALERT_DAYS
    low_items = _build_low_stock_items()

    if not low_items:
        return False

    lines_parts = []
    for item in low_items:
        sizes = ", ".join(f"T{s['size']}: {s['stock']}u ({s['info']})" for s in item["low_sizes"])
        lines_parts.append(f"- {item['name']} ({item['code']}) — total {item['total_stock']}u — {sizes}")
    lines = "\n".join(lines_parts)
    body = f"""Rapport de stock bas — {timezone.now().strftime('%d/%m/%Y %H:%M')}

Les produits suivants ont un stock bas (alerte si moins de {ALERT_DAYS} jours de couverture) :

{lines}

Connectez-vous pour voir les détails : https://web-production-1391c5.up.railway.app/products/
"""
    return _send_email(
        subject=f"⚠ Stock bas — {len(low_items)} produit(s) à réapprovisionner",
        body=body,
    )


def _send_daily_summary_email():
    """Send daily scan summary email."""
    today = get_scan_session_date()  # rolls Saturday session over Sunday
    # Today's session — dedupe per bordereau (latest row per barcode wins)
    from .models import ScanSessionLog
    logs = ScanSessionLog.objects.filter(session_date=today).order_by("-scanned_at")
    seen = set()
    correct = 0
    wrong = 0
    for log in logs:
        if log.bordereau_barcode in seen:
            continue
        seen.add(log.bordereau_barcode)
        if log.is_correct:
            correct += 1
        else:
            wrong += 1

    # En attente from Navex (approx from our DB)
    import urllib.request, urllib.parse
    navex_count = "N/A"
    try:
        data = urllib.parse.urlencode({"getattente": "1"}).encode()
        req = urllib.request.Request(
            NAVEX_API_URL,
            data=data, method="POST"
        )
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=10) as resp:
            import json as json_lib
            navex_data = json_lib.loads(resp.read().decode())
        our_barcodes = set(ShippingOrder.objects.filter(status__in=(ShippingOrder.CLOSED, ShippingOrder.PAID)).values_list("bordereau_barcode", flat=True))
        navex_count = sum(1 for c in navex_data.get("colis", []) if c.get("code_barre") not in our_barcodes)
    except Exception:
        pass

    # À vérifier count
    from .models import OrderVerification
    now = timezone.now()
    verifier_count = 0
    for order in ShippingOrder.objects.filter(status__in=(ShippingOrder.CLOSED, ShippingOrder.PARTIAL_RETURNED)):
        if order.closed_at:
            weekday = order.closed_at.weekday()
            limit = 58 if weekday == 5 else 34
            if (now - order.closed_at).total_seconds() / 3600 >= limit:
                verifier_count += 1

    body = f"""Résumé de la journée — {today.strftime('%d/%m/%Y')}

📦 Ordres scannés aujourd'hui :
  ✓ Corrects : {correct}
  ✗ À vérifier : {wrong}

⏳ En attente Navex (non encore scannés) : {navex_count}

⚠ Ordres en retard de livraison : {verifier_count}

Connectez-vous pour voir les détails : https://web-production-1391c5.up.railway.app/
"""
    return _send_email(
        subject=f"📊 Résumé du {today.strftime('%d/%m/%Y')} — {correct} ordres scannés",
        body=body,
    )


def _send_a_verifier_email():
    """Send À vérifier alert email."""
    now = timezone.now()
    late_orders = []
    for order in ShippingOrder.objects.filter(status__in=(ShippingOrder.CLOSED, ShippingOrder.PARTIAL_RETURNED)):
        if order.closed_at:
            weekday = order.closed_at.weekday()
            limit = 58 if weekday == 5 else 34
            hours = (now - order.closed_at).total_seconds() / 3600
            if hours >= limit:
                late_orders.append({"bc": order.bordereau_barcode, "hours": round(hours, 1), "closed": order.closed_at.strftime("%d/%m/%Y %H:%M")})

    if not late_orders:
        return False

    lines = "\n".join([f"- {o['bc']} — expédié le {o['closed']} ({o['hours']}h sans livraison)" for o in late_orders])
    body = f"""Alerte — Ordres en retard de livraison — {now.strftime('%d/%m/%Y %H:%M')}

{len(late_orders)} ordre(s) expédiés depuis plus de 34h sans confirmation de livraison :

{lines}

Vérifiez ces ordres : https://web-production-1391c5.up.railway.app/a-verifier/
"""
    return _send_email(
        subject=f"⚠ {len(late_orders)} ordre(s) en retard de livraison",
        body=body,
    )


# Manual email send endpoints
@csrf_exempt
def api_send_email(request, email_type):
    if not request.user.is_superuser:
        return JsonResponse({"status": "error", "message": "Admin uniquement."})
    
    try:
        if email_type == "low_stock":
            # Send the low-stock report to Telegram (family-aware, with photos)
            # instead of email.
            result = _send_low_stock_whatsapp()
            msg = "Alerte stock bas envoyée sur Telegram !" if result else "Aucun produit en stock bas."
        elif email_type == "daily_summary":
            result = _send_daily_summary_email()
            msg = "Résumé quotidien envoyé !"
        elif email_type == "a_verifier":
            result = _send_a_verifier_email()
            msg = "Email À vérifier envoyé !" if result else "Aucun ordre en retard."
        else:
            return JsonResponse({"status": "error", "message": "Type inconnu."})
        return JsonResponse({"status": "ok", "message": msg})
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)})


# Cron endpoints (called by Railway cron)
def cron_morning_email(request):
    """Called at 11am by Railway cron."""
    _send_low_stock_email()
    _send_low_stock_whatsapp()
    return JsonResponse({"status": "ok", "type": "morning"})


@csrf_exempt
def test_low_stock_whatsapp(request):
    """Manual trigger to test the low-stock alert now. Add ?force=1 to send a
    sample alert (first product with an image) even if nothing is low."""
    if request.GET.get("force") == "1":
        # Build a sample item from the first variant that has an image.
        v = ProductVariant.objects.exclude(image="").exclude(image__isnull=True).select_related("product").first()
        if not v:
            _send_telegram("\U0001F9EA Test Telegram : aucune image produit trouvée, mais l'envoi texte fonctionne.")
            return JsonResponse({"status": "ok", "note": "sent text test, no product image found"})
        caption = (
            f"\U0001F9EA TEST — alerte stock bas (exemple)\n"
            f"\U0001F4E6 {v.product.name} {v.color_label}\n"
            f"Code : {v.product.code}\n"
            f"Stock restant : 3 unités\n"
            f"\u23F3 ~4j restants à 0.7/jour"
        )
        ok = _send_telegram_photo(v.image.path, caption)
        if not ok:
            _send_telegram(caption)
        return JsonResponse({"status": "ok", "forced": True, "photo_sent": ok})
    sent = _send_low_stock_whatsapp()
    n = len(_build_low_stock_items())
    return JsonResponse({"status": "ok", "sent": sent, "low_items": n})


def cron_evening_email(request):
    """Called at 7pm by Railway cron."""
    _send_daily_summary_email()
    _send_a_verifier_email()
    return JsonResponse({"status": "ok", "type": "evening"})


@csrf_exempt
def cron_navex_sync(request):
    """Called hourly during work hours by Railway cron to refresh Navex
    statuses for pending v2 orders. No auth (Railway cron hits it directly);
    it only triggers a read-sync, no destructive action."""
    try:
        n_attempted, n_updated = _sync_navex_for_v2_orders(only_pending=True)
        try:
            from .models import AuditLog as _AL
            _AL.objects.create(
                user=None, username="system_cron", action=_AL.NAVEX_SYNC,
                description=f"Sync auto Navex (cron horaire): {n_updated}/{n_attempted} mis à jour",
            )
        except Exception:
            pass
        return JsonResponse({"status": "ok", "attempted": n_attempted, "updated": n_updated})
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)[:200]}, status=500)


def navex_sync(request):
    """Sync page — shows all shipped orders with their Navex status."""
    if not request.user.is_staff:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("Accès réservé.")
    shipped_orders = ShippingOrder.objects.filter(
        status=ShippingOrder.CLOSED
    ).order_by("-opened_at")
    return render(request, "inventory/navex_sync.html", {
        "shipped_orders": shipped_orders,
    })


@csrf_exempt
def api_navex_sync(request):
    """Call Navex API for multiple barcodes at once."""
    from .models import log_action, AuditLog, Order
    import urllib.request
    import urllib.parse

    # Get all shipped/closed order barcodes
    # Only check CLOSED orders — not yet paid in our system
    orders = ShippingOrder.objects.filter(
        status=ShippingOrder.CLOSED
    ).values("id", "bordereau_barcode", "status", "amount_collected", "opened_at")

    if not orders:
        return JsonResponse({"status": "ok", "results": []})

    log_action(
        request.user, AuditLog.NAVEX_SYNC,
        description=f"Sync Navex lancé sur {len(orders)} ordre(s) CLOSED",
        request=request,
    )

    codes = [o["bordereau_barcode"] for o in orders]
    codes_string = ", ".join(codes)

    try:
        data = urllib.parse.urlencode({"codes": codes_string, "include_prix": "1"}).encode()
        req = urllib.request.Request(
            NAVEX_API_URL,
            data=data,
            method="POST"
        )
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=15) as response:
            import json as json_lib
            navex_data = json_lib.loads(response.read().decode())

        # Build a map of code → navex result
        navex_map = {}
        for result in navex_data.get("results", []):
            navex_map[result["code"]] = result

        # Merge our orders with navex data
        merged = []
        n_early_returned = 0
        n_returned_confirmed = 0
        for order in orders:
            bc = order["bordereau_barcode"]
            navex = navex_map.get(bc, None)
            navex_etat = navex.get("etat", "Introuvable") if navex and navex.get("status") == 1 else "Introuvable"
            needs_attention = navex_etat in ("Livrer Paye", "Livré", "Livrée", "Livré Payé")
            is_anomaly = navex_etat in ("Retourné", "Retourne", "Annulé", "Annule")

            # Auto-flip ProductUnit statuses for v1 ShippingOrder based on Navex etat.
            # Workflow:
            #   SHIPPED → EARLY_RETURN  (Navex "Rtn client/agence" — customer refused, on way back)
            #   EARLY_RETURN → AT_DEPOT  (Navex "Retour Depot Navex" — arrived at hub, waiting for our pickup)
            #   AT_DEPOT → RETURNED      (we physically scan it back at our warehouse — done elsewhere)
            navex_lower = navex_etat.strip().lower()
            try:
                so = ShippingOrder.objects.get(pk=order["id"])
                if navex_lower in ("rtn client/agence", "rtn client", "rtn agence", "retour anticipe", "retour anticipé"):
                    flipped = 0
                    for item in so.items.select_related("unit"):
                        if item.unit and item.unit.status == ProductUnit.SHIPPED:
                            item.unit.status = ProductUnit.EARLY_RETURN
                            item.unit.save(update_fields=["status", "updated_at"])
                            StockMovement.objects.create(
                                unit=item.unit,
                                movement_type=StockMovement.EARLY_RETURN,
                                reference=f"Auto sync Navex — bordereau {bc}",
                                user=request.user,
                            )
                            flipped += 1
                    if flipped:
                        n_early_returned += flipped
                        log_action(
                            request.user, AuditLog.STATUS_CHANGE,
                            description=f"Auto sync Navex: {flipped} unité(s) SHIPPED → EARLY_RETURN (bordereau {bc}, etat '{navex_etat}')",
                            request=request,
                        )
                elif navex_lower in ("retour recu", "retour reçu", "retourne", "retourné", "retour confirme", "retour confirmé"):
                    # Unit has arrived at Navex hub, waiting for our physical pickup.
                    # Flip SHIPPED and EARLY_RETURN units → AT_DEPOT.
                    flipped = 0
                    for item in so.items.select_related("unit"):
                        if item.unit and item.unit.status in (ProductUnit.SHIPPED, ProductUnit.EARLY_RETURN):
                            item.unit.status = ProductUnit.AT_DEPOT
                            item.unit.save(update_fields=["status", "updated_at"])
                            StockMovement.objects.create(
                                unit=item.unit,
                                movement_type=StockMovement.AT_DEPOT,
                                reference=f"Auto sync Navex — bordereau {bc}",
                                user=request.user,
                            )
                            flipped += 1
                    if flipped:
                        log_action(
                            request.user, AuditLog.STATUS_CHANGE,
                            description=f"Auto sync Navex: {flipped} unité(s) → AT_DEPOT (bordereau {bc}, etat '{navex_etat}')",
                            request=request,
                        )

                # v2 Order status: Navex "Au magasin" / "En cours" / return
                # states → move the linked v2 Order into that status. Forward
                # states only from Confirmée; "En retour" (Navex "Retour
                # Expéditeur" or "Rtn client/agence") from any in-transit state.
                # NOTE: "Retourné" (final) is NOT set here — it is set when the
                # return is physically scanned in v1.
                linked_order = getattr(so, "order", None)
                if linked_order:
                    new_v2_status = None
                    if linked_order.status == Order.CONFIRMEE:
                        if navex_lower in ("au magasin", "au-magasin", "au magasin navex"):
                            new_v2_status = Order.AU_MAGASIN
                        elif navex_lower in ("en cours", "en-cours", "en cours de livraison"):
                            new_v2_status = Order.EN_COURS
                    if linked_order.status in (Order.CONFIRMEE, Order.EN_COURS, Order.AU_MAGASIN):
                        if navex_lower in ("retour expediteur", "retour expéditeur",
                                           "retour vers expediteur", "retour vers expéditeur",
                                           "rtn client/agence", "rtn client", "rtn agence"):
                            new_v2_status = Order.RETURNING
                    if new_v2_status and new_v2_status != linked_order.status:
                        old_label = dict(Order.STATUS_CHOICES).get(linked_order.status, linked_order.status)
                        linked_order.status = new_v2_status
                        linked_order.save(update_fields=["status", "updated_at"])
                        _maybe_send_status_sms(linked_order)
                        log_action(
                            request.user, AuditLog.STATUS_CHANGE,
                            description=f"Auto sync Navex: commande #{linked_order.id} {old_label} → "
                                        f"{dict(Order.STATUS_CHOICES)[new_v2_status]} (bordereau {bc}, etat '{navex_etat}')",
                            request=request,
                        )
            except ShippingOrder.DoesNotExist:
                pass

            # Check if order is closed for more than 24h and not yet delivered
            if not needs_attention and not is_anomaly:
                try:
                    order_obj = ShippingOrder.objects.get(pk=order["id"])
                    if order_obj.closed_at:
                        hours_since_close = (timezone.now() - order_obj.closed_at).total_seconds() / 3600
                        if hours_since_close > 24:
                            is_anomaly = True
                except Exception:
                    pass
            # Get price from Navex (include_prix=1) - field is "prix"
            navex_prix = navex.get("prix") if navex else None
            # 'Notre total' = the Navex price we saved on the order at scan time
            # (api_save_navex_info writes the Navex 'prix' to order.amount_collected).
            # We no longer recompute from sell_price + 7 — that would double-count
            # delivery and cause spurious mismatches with Navex's price.
            try:
                order_obj = ShippingOrder.objects.get(pk=order["id"])
                order_items = list(order_obj.items.select_related("unit__variant__product"))
                unit_count = len(order_items)
                # 'Notre total' should reflect OUR price (from our offers on the
                # linked v2 Order), not the Navex price. Prefer the v2 Order
                # total; fall back to the saved amount_collected only if there's
                # no v2 order. 0 is a valid total (e.g. exchanges) — keep it.
                our_total = None
                _v2o = None
                if order_obj.order_id:
                    from .models import Order as _OV2
                    _v2o = _OV2.objects.filter(pk=order_obj.order_id).only("total").first()
                if _v2o is None and bc:
                    from .models import Order as _OV2
                    _v2o = _OV2.objects.filter(bordereau_barcode=bc).only("total").first()
                if _v2o is not None:
                    our_total = _v2o.total
                else:
                    our_total = order_obj.amount_collected
            except Exception:
                our_total = None
                unit_count = 0

            price_match = None
            if navex_prix is not None and our_total is not None:
                try:
                    price_match = abs(Decimal(str(navex_prix)) - Decimal(str(our_total))) < Decimal("0.1")
                except Exception:
                    price_match = None

            # Calculate hours since close for display
            hours_late = None
            v2_order_id = None
            try:
                order_obj2 = ShippingOrder.objects.get(pk=order["id"])
                if order_obj2.closed_at:
                    hours_late = round((timezone.now() - order_obj2.closed_at).total_seconds() / 3600, 1)
                # v2 Order linked to this shipping order (if any), so the barcode
                # can offer "office (v2)" alongside "shipping (v1)".
                if order_obj2.order_id:
                    v2_order_id = order_obj2.order_id
            except Exception:
                pass
            # Fallback: match a v2 Order by the same bordereau barcode.
            if v2_order_id is None and bc:
                try:
                    from .models import Order as _OrderV2
                    _v2 = _OrderV2.objects.filter(bordereau_barcode=bc).only("id").first()
                    if _v2:
                        v2_order_id = _v2.id
                except Exception:
                    pass

            merged.append({
                "id": order["id"],
                "v2_order_id": v2_order_id,
                "bordereau_barcode": bc,
                "our_status": order["status"],
                "amount_collected": str(order["amount_collected"] or ""),
                "navex_etat": navex_etat,
                "navex_motif": navex.get("motif", "") if navex else "",
                "navex_livreur": navex.get("livreur", "") if navex else "",
                "navex_prix": str(navex_prix) if navex_prix else None,
                "our_total": (str(our_total) if our_total is not None else None),
                "unit_count": unit_count,
                "price_match": price_match,
                "needs_attention": needs_attention,
                "is_anomaly": is_anomaly,
                "hours_late": hours_late,
                "opened_at": order["opened_at"].strftime("%d/%m/%Y %H:%M") if order["opened_at"] else "",
            })

        # Sort: needs attention first
        merged.sort(key=lambda x: (not x["needs_attention"], x["opened_at"]))

        return JsonResponse({"status": "ok", "results": merged, "total": len(merged)})

    except Exception as e:
        return JsonResponse({"status": "error", "message": f"Erreur Navex : {str(e)}"})


def products_list(request):
    show_archived = request.GET.get("archived") == "1"
    season = request.GET.get("season", "")  # 'summer', 'winter', or '' (= bubble landing)

    # If no season picked and not viewing archived → show the bubble landing page
    if not season and not show_archived:
        summer_count = Product.objects.filter(archived=False, season=Product.SEASON_SUMMER).count()
        winter_count = Product.objects.filter(archived=False, season=Product.SEASON_WINTER).count()
        archived_count = Product.objects.filter(archived=True).count()
        return render(request, "inventory/products_landing.html", {
            "summer_count": summer_count,
            "winter_count": winter_count,
            "archived_count": archived_count,
        })

    products_qs = Product.objects.prefetch_related("variants__units")
    if show_archived:
        products_qs = products_qs.filter(archived=True)
    else:
        products_qs = products_qs.filter(archived=False)
        if season in (Product.SEASON_SUMMER, Product.SEASON_WINTER):
            products_qs = products_qs.filter(season=season)
    # Top level shows only PARENTS + standalone products (hide V2/V3 children;
    # they appear nested under their parent when expanded).
    products = products_qs.filter(parent_product__isnull=True).all()
    total_available = ProductUnit.objects.filter(status__in=(ProductUnit.IN_STOCK, ProductUnit.RETURNED)).count()
    total_shipped = ProductUnit.objects.filter(status=ProductUnit.SHIPPED).count()
    total_paid = ProductUnit.objects.filter(status=ProductUnit.PAID).count()
    total_early_return = ProductUnit.objects.filter(status=ProductUnit.EARLY_RETURN).count()
    total_at_depot = ProductUnit.objects.filter(status=ProductUnit.AT_DEPOT).count()

    from .models import compute_size_forecast, compute_family_size_forecast
    from django.db.models import Q

    def _stock_breakdown(prod):
        """Per-product stock counts + low/zero sizes (its own units only)."""
        in_stock_count = returned_count = early_return_count = at_depot_count = 0
        for variant in prod.variants.all():
            for unit in variant.units.all():
                if unit.status == ProductUnit.IN_STOCK:
                    in_stock_count += 1
                elif unit.status == ProductUnit.RETURNED:
                    returned_count += 1
                elif unit.status == ProductUnit.EARLY_RETURN:
                    early_return_count += 1
                elif unit.status == ProductUnit.AT_DEPOT:
                    at_depot_count += 1
        return {
            "product": prod,
            "stock": in_stock_count + returned_count,
            "in_stock_count": in_stock_count,
            "returned_count": returned_count,
            "early_return_count": early_return_count,
            "at_depot_count": at_depot_count,
            "variants": prod.variants.all(),
        }

    # Calculate low stock sizes per product (predictive: days-of-cover < 10)
    products_data = []
    for product in products:
        # The family = this parent + its versions.
        family = list(Product.objects.filter(
            Q(id=product.id) | Q(parent_product=product)
        ).prefetch_related("variants__units"))
        fam_ids = [p.id for p in family]
        has_children = len(family) > 1

        # Family-combined stock counts.
        in_stock_count = returned_count = early_return_count = at_depot_count = 0
        for fp in family:
            for variant in fp.variants.all():
                for unit in variant.units.all():
                    if unit.status == ProductUnit.IN_STOCK:
                        in_stock_count += 1
                    elif unit.status == ProductUnit.RETURNED:
                        returned_count += 1
                    elif unit.status == ProductUnit.EARLY_RETURN:
                        early_return_count += 1
                    elif unit.status == ProductUnit.AT_DEPOT:
                        at_depot_count += 1
        stock = in_stock_count + returned_count
        is_low = stock <= product.alert_threshold and not product.alert_disabled

        # Low/zero sizes computed across the WHOLE family per size.
        low_sizes = []
        zero_sizes = []
        if not product.alert_disabled:
            all_sizes = set(
                ProductUnit.objects.filter(variant__product_id__in=fam_ids)
                .values_list("size", flat=True).distinct()
            )
            for size in all_sizes:
                if not size:
                    continue
                f = compute_family_size_forecast(fam_ids, size)
                if f["current_stock"] == 0:
                    zero_sizes.append(size)
                elif f["is_triggered"]:
                    low_sizes.append(size)

        # Per-version breakdown (only if it has children) for the expand panel.
        children_data = []
        if has_children:
            for fp in family:
                children_data.append(_stock_breakdown(fp))

        # Average units that became PAID per day across the whole family over
        # the last FORECAST_WINDOW_DAYS. Source = PAID StockMovements (each one
        # = a unit that got paid), dated by moved_at.
        from .models import StockMovement, FORECAST_WINDOW_DAYS
        cutoff = timezone.now() - timezone.timedelta(days=FORECAST_WINDOW_DAYS)
        paid_qty = StockMovement.objects.filter(
            unit__variant__product_id__in=fam_ids,
            movement_type=StockMovement.PAID,
            moved_at__gte=cutoff,
        ).count()
        avg_per_day = paid_qty / float(FORECAST_WINDOW_DAYS)

        products_data.append({
            "product": product,
            "stock": stock,
            "in_stock_count": in_stock_count,
            "returned_count": returned_count,
            "early_return_count": early_return_count,
            "at_depot_count": at_depot_count,
            "low_sizes": low_sizes,
            "zero_sizes": zero_sizes,
            "is_low": is_low,
            "variants": product.variants.all(),
            "has_children": has_children,
            "version_count": len(family),
            "children_data": children_data,
            "avg_per_day": round(avg_per_day, 1),
        })

    # Sort by best-selling (highest avg/day first).
    products_data.sort(key=lambda d: d["avg_per_day"], reverse=True)

    return render(request, "inventory/products_list.html", {
        "products_data": products_data,
        "products": products,
        "total_available": total_available,
        "total_shipped": total_shipped,
        "total_paid": total_paid,
        "total_early_return": total_early_return,
        "total_at_depot": total_at_depot,
        "show_archived": show_archived,
        "season": season,
        "archived_count": Product.objects.filter(archived=True).count(),
    })


@login_required(login_url="/login/")
def product_detail(request, pk):
    product = get_object_or_404(Product, pk=pk)
    from .models import compute_size_forecast
    from django.db.models import Q

    # Resolve the SKU family: the root (parent or self) + all its versions.
    root = product.parent_product or product
    family = list(
        Product.objects.filter(Q(id=root.id) | Q(parent_product=root))
        .prefetch_related("variants__units")
    )
    # Order: root first, then versions by name.
    family.sort(key=lambda p: (p.id != root.id, p.name))

    # Filter buttons data (always show the root product's page; the buttons just
    # change which scope we display).
    versions = [{"id": p.id, "name": p.name, "code": p.code, "is_root": p.id == root.id}
                for p in family]

    # Which scope are we showing? ?version=<id> for a single version, or 'all'
    # (default) for the whole family combined.
    sel = request.GET.get("version", "all")
    if sel != "all":
        try:
            sel_id = int(sel)
            scope_products = [p for p in family if p.id == sel_id] or family
        except ValueError:
            sel = "all"; scope_products = family
    else:
        scope_products = family

    # Collect variants across the scoped products.
    variants = []
    for p in scope_products:
        variants.extend(p.variants.prefetch_related("units").all())

    # Build size breakdown per variant.
    variants_data = []
    for variant in variants:
        all_sizes = list(variant.units.values_list("size", flat=True).distinct())
        size_map = {s: 0 for s in all_sizes}
        for unit in variant.units.filter(status__in=(ProductUnit.IN_STOCK, ProductUnit.RETURNED)):
            size_map[unit.size] = size_map.get(unit.size, 0) + 1
        size_forecasts = {s: compute_size_forecast(variant, s) for s in size_map.keys()}
        variants_data.append({
            "variant": variant,
            "size_map": size_map,
            "size_forecasts": size_forecasts,
            "total_stock": variant.total_stock,
            "all_units": variant.units.all(),
        })

    # In COMBINED ("all") mode with multiple versions, merge variants that share
    # the same colour so each colour shows ONCE with summed sizes — instead of a
    # separate card per version (BLUE-v1, BLUE-v2). Single-version pages and
    # per-version views keep the raw per-variant cards.
    if sel == "all" and len(family) > 1:
        merged = {}   # color_label -> aggregated entry
        order = []
        for vd in variants_data:
            v = vd["variant"]
            key = (v.color_label or v.color_name or "—").strip().lower()
            if key not in merged:
                merged[key] = {
                    "variant": v,                       # representative (for color/name/links)
                    "size_map": dict(vd["size_map"]),
                    "size_forecasts": dict(vd["size_forecasts"]),
                    "total_stock": vd["total_stock"],
                    "all_units": list(vd["all_units"]),
                    "merged_variant_ids": [v.id],
                }
                order.append(key)
            else:
                m = merged[key]
                for s, n in vd["size_map"].items():
                    m["size_map"][s] = m["size_map"].get(s, 0) + n
                for s, f in vd["size_forecasts"].items():
                    m["size_forecasts"].setdefault(s, f)
                m["total_stock"] += vd["total_stock"]
                m["all_units"].extend(vd["all_units"])
                m["merged_variant_ids"].append(v.id)
        variants_data = [merged[k] for k in order]

    # Stock totals for the scoped products.
    in_stock_total = returned_total = early_return_total = at_depot_total = 0
    for p in scope_products:
        for variant in p.variants.all():
            for unit in variant.units.all():
                if unit.status == ProductUnit.IN_STOCK:
                    in_stock_total += 1
                elif unit.status == ProductUnit.RETURNED:
                    returned_total += 1
                elif unit.status == ProductUnit.EARLY_RETURN:
                    early_return_total += 1
                elif unit.status == ProductUnit.AT_DEPOT:
                    at_depot_total += 1
    available_total = in_stock_total + returned_total

    # Stock-added history: group the scoped products' units by the DAY they were
    # created (= when stock was added), with a per-color breakdown, newest first.
    added_map = {}   # date -> {color_label -> count}
    for p in scope_products:
        for variant in p.variants.all():
            color = variant.color_label or variant.color_name or "—"
            for unit in variant.units.all():
                d = timezone.localtime(unit.created_at).date()
                if d not in added_map:
                    added_map[d] = {}
                added_map[d][color] = added_map[d].get(color, 0) + 1
    stock_added = []
    for d in sorted(added_map.keys(), reverse=True):
        colors = added_map[d]
        stock_added.append({
            "date": d,
            "count": sum(colors.values()),
            "colors": [{"color": c, "count": n}
                       for c, n in sorted(colors.items(), key=lambda x: -x[1])],
        })

    return render(request, "inventory/product_detail.html", {
        "product": product,
        "root_product": root,
        "variants": variants,
        "variants_data": variants_data,
        "in_stock_total": in_stock_total,
        "returned_total": returned_total,
        "early_return_total": early_return_total,
        "at_depot_total": at_depot_total,
        "available_total": available_total,
        "versions": versions,
        "selected_version": sel,
        "has_family": len(family) > 1,
        "stock_added": stock_added,
    })


@csrf_exempt
@require_POST
@login_required(login_url="/login/")
def api_toggle_product_flag(request, pk):
    """Toggle alert_disabled or archived on a product. Admin-only."""
    if not request.user.is_superuser:
        return JsonResponse({"status": "error", "message": "Accès refusé."}, status=403)
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "JSON invalide."}, status=400)
    flag = data.get("flag")
    if flag not in ("alert_disabled", "archived"):
        return JsonResponse({"status": "error", "message": "Flag invalide."}, status=400)
    product = get_object_or_404(Product, pk=pk)
    old_value = getattr(product, flag)
    new_value = bool(data.get("value", not old_value))
    setattr(product, flag, new_value)
    product.save(update_fields=[flag])

    from .models import log_action, AuditLog
    log_action(
        request.user, AuditLog.EDIT,
        description=f"Produit '{product.name}' : {flag} {old_value} → {new_value}",
        request=request,
        target_model="Product", target_id=product.id,
    )

    return JsonResponse({
        "status": "ok",
        "flag": flag,
        "value": new_value,
        "alert_disabled": product.alert_disabled,
        "archived": product.archived,
    })


@login_required(login_url="/login/")
def api_check_barcode_gaps(request, pk):
    """Find gaps in unit barcode numeric sequences, grouped by (color, size).

    Barcode pattern: <PREFIX>-<COLOR>-<SIZE>-<NUMBER>  e.g. PTSICY-WTE-3-016
    For each (color, size) group we extract the trailing number and report
    any missing integers between min and max.
    """
    import re
    product = get_object_or_404(Product, pk=pk)
    from django.db.models import Q

    # Optional ?variant=ID & ?size=N filtering for per-variant button
    variant_id = request.GET.get("variant")
    size_filter = request.GET.get("size")
    version = request.GET.get("version", "all")  # 'all' = whole family, or a product id

    if variant_id:
        # Single variant: just that variant.
        variants = ProductVariant.objects.filter(pk=variant_id).prefetch_related("units")
    else:
        # Determine the scope of products to check.
        root = product.parent_product or product
        family = Product.objects.filter(Q(id=root.id) | Q(parent_product=root))
        if version and version != "all":
            try:
                scope_ids = [int(version)]
            except ValueError:
                scope_ids = list(family.values_list("id", flat=True))
        else:
            scope_ids = list(family.values_list("id", flat=True))
        variants = ProductVariant.objects.filter(
            product_id__in=scope_ids
        ).prefetch_related("units")

    groups = []  # one dict per (variant, size)
    for variant in variants:
        # Group units by size
        size_buckets = {}
        for unit in variant.units.all():
            if size_filter and str(unit.size) != str(size_filter):
                continue
            size_buckets.setdefault(unit.size, []).append(unit.barcode)

        for size, barcodes in size_buckets.items():
            numbers = []
            for bc in barcodes:
                m = re.search(r"(\d+)$", bc or "")
                if m:
                    numbers.append(int(m.group(1)))
            if not numbers:
                continue
            numbers_set = set(numbers)
            lo, hi = min(numbers), max(numbers)
            missing = [n for n in range(lo, hi + 1) if n not in numbers_set]

            groups.append({
                "variant_id": variant.pk,
                "color": variant.color_label,
                "size": size,
                "count": len(numbers),
                "range_lo": lo,
                "range_hi": hi,
                "missing": missing,
                "missing_count": len(missing),
            })

    # Sort: variants by color, then by size
    groups.sort(key=lambda g: (g["color"], str(g["size"])))

    total_missing = sum(g["missing_count"] for g in groups)
    return JsonResponse({
        "status": "ok",
        "product": product.name,
        "total_missing": total_missing,
        "groups": groups,
    })


# ---------------------------------------------------------------------------
# V2 — ORDER MANAGEMENT (Phase 4)
# Customer order creation. Independent from the existing scan/Navex flow.
# Once an order is "Confirmée", a later phase will push it to Navex and
# fill in the bordereau_barcode.
# ---------------------------------------------------------------------------

def _orders_role_check(request):
    """Office, Shipping, and Admins may access. Messages Team cannot."""
    if not request.user.is_authenticated:
        return False
    if request.user.is_superuser:
        return True
    try:
        return request.user.profile.role != "messages"
    except Exception:
        return True  # legacy users default to allowed


@login_required(login_url="/login/")
def orders_list(request):
    if not _orders_role_check(request):
        return redirect("home")
    from .models import Order, SalesPage, Region
    from datetime import date as _date
    from django.db.models import Q
    # Default behavior: show "non_confirmee" orders only.
    # User must explicitly pick a filter (or "all") to see other orders.
    status_filter = request.GET.get("status", None)
    if status_filter is None:
        status_filter = "non_confirmee"

    qs = Order.objects.select_related("customer", "region", "sales_page").prefetch_related(
        "lines__product", "order_offers", "shipping_orders"
    )

    # Source filter: barats.tn (website), converty, or facebook (every other
    # page). Default = all sources.
    source_filter = request.GET.get("source", "all")
    from django.db.models import Q as _Q
    if source_filter == "barats":
        qs = qs.filter(sales_page__name__iexact="Barats.tn")
    elif source_filter == "converty":
        qs = qs.filter(sales_page__name__iexact="Converty")
    elif source_filter == "facebook":
        qs = qs.exclude(sales_page__name__iexact="Barats.tn").exclude(sales_page__name__iexact="Converty")

    # "Aujourd'hui" toggle. When ON, it narrows ONLY the early/call-center
    # statuses to today's business day (17h yesterday → 17h today, by création
    # date). The outcome statuses (en cours, au magasin, en retour, livrée,
    # payée) always show ALL dates and ignore the toggle.
    import datetime as _dt
    try:
        import zoneinfo
        _tz = zoneinfo.ZoneInfo("Africa/Tunis")
    except Exception:
        _tz = timezone.get_current_timezone()

    today_on = request.GET.get("today") == "1"
    EARLY_STATUSES = {"non_confirmee", "confirmee", "rappeler_plus_tard", "injoignable", "pas_serieux", "annulee"}
    today_start = today_end = None
    if today_on:
        today_local = timezone.localdate()
        today_end = timezone.make_aware(_dt.datetime.combine(today_local, _dt.time(17, 0)), _tz)
        today_start = today_end - _dt.timedelta(days=1)

    if status_filter and status_filter != "all":
        qs = qs.filter(status=status_filter)

    # Apply the today window only to early statuses.
    if today_on and today_start and (status_filter in EARLY_STATUSES or status_filter == "all"):
        if status_filter == "all":
            # For "Toutes": early statuses limited to today, outcome statuses all.
            qs = qs.filter(Q(created_at__gte=today_start, created_at__lt=today_end)
                           | ~Q(status__in=EARLY_STATUSES))
        else:
            qs = qs.filter(created_at__gte=today_start, created_at__lt=today_end)

    # Hide orders scheduled for a future date (today's not-yet-actionable orders).
    # NULL scheduled_for = always visible. scheduled_for <= today = visible.
    # User can pass ?show_scheduled=1 to bypass this filter.
    today = _date.today()
    if request.GET.get("show_scheduled") != "1":
        qs = qs.filter(Q(scheduled_for__isnull=True) | Q(scheduled_for__lte=today))

    orders = qs[:500]

    # For each displayed order, how many TOTAL v2 orders share the same phone
    # number (i.e. same customer). Shown as a badge so staff can spot repeat
    # customers / possible duplicates. Computed in one query, no N+1.
    from django.db.models import Count as _Count
    customer_ids = {o.customer_id for o in orders if o.customer_id}
    order_counts = {}
    insystem_delivered = {}
    insystem_returned = {}
    phone_by_cust = {}
    if customer_ids:
        for row in (Order.objects.filter(customer_id__in=customer_ids)
                    .values("customer_id").annotate(n=_Count("id"))):
            order_counts[row["customer_id"]] = row["n"]
        # In-system delivered (livree+payee) and returned per customer.
        for row in (Order.objects.filter(customer_id__in=customer_ids, status__in=["livree", "payee"])
                    .values("customer_id").annotate(n=_Count("id"))):
            insystem_delivered[row["customer_id"]] = row["n"]
        for row in (Order.objects.filter(customer_id__in=customer_ids, status="returned")
                    .values("customer_id").annotate(n=_Count("id"))):
            insystem_returned[row["customer_id"]] = row["n"]

    # Historic stats per phone (seeded from Navex exports). Defensive: if the
    # table doesn't exist yet (migration not applied), skip silently.
    from .models import Customer as _Cust, CustomerHistory as _CH
    phones = dict(_Cust.objects.filter(id__in=customer_ids).values_list("id", "phone"))
    hist = {}
    try:
        hist = {h.phone: h for h in _CH.objects.filter(phone__in=phones.values())}
    except Exception:
        hist = {}

    for o in orders:
        live_count = order_counts.get(o.customer_id, 1)
        ph = phones.get(o.customer_id)
        h = hist.get(ph) if ph else None
        hist_total = h.historic_total if h else 0
        hist_deliv = h.historic_delivered if h else 0
        hist_ret = h.historic_returned if h else 0
        # Combined badge count = live orders + historic orders.
        o.phone_order_count = live_count + hist_total
        # Combined delivered / returned (annulé excluded everywhere).
        o.combined_delivered = insystem_delivered.get(o.customer_id, 0) + hist_deliv
        o.combined_returned = insystem_returned.get(o.customer_id, 0) + hist_ret
        o.has_history = hist_total > 0
        # VIP: a customer with 3+ delivered orders (livrée/payée + historic).
        o.is_vip = o.combined_delivered >= 3
        # Risky: more returned orders than delivered (returns > livrées).
        o.is_risky = o.combined_returned > o.combined_delivered and o.combined_returned > 0

    # Count of currently-hidden future-scheduled orders, for an info banner
    future_count = Order.objects.filter(scheduled_for__gt=today).count()

    from django.db.models import Count
    # Chip counts respect the source filter (but not the status filter, so each
    # chip shows its own total within the chosen source).
    counts_qs = Order.objects.all()
    if source_filter == "barats":
        counts_qs = counts_qs.filter(sales_page__name__iexact="Barats.tn")
    elif source_filter == "converty":
        counts_qs = counts_qs.filter(sales_page__name__iexact="Converty")
    elif source_filter == "facebook":
        counts_qs = counts_qs.exclude(sales_page__name__iexact="Barats.tn").exclude(sales_page__name__iexact="Converty")
    # Chip counts: when 'today' is on, early statuses are limited to today's
    # window; outcome statuses always count all.
    if today_on and today_start:
        counts_qs = counts_qs.filter(
            Q(created_at__gte=today_start, created_at__lt=today_end)
            | ~Q(status__in=EARLY_STATUSES)
        )
    counts = dict(counts_qs.values_list("status").annotate(n=Count("id")))
    source_total = counts_qs.count()

    # If ?create_exchange=ID is in the URL, fetch the original order so the
    # template can pre-fill the inline editor for an exchange.
    exchange_source = None
    create_exchange_id = request.GET.get("create_exchange")
    if create_exchange_id:
        try:
            src = Order.objects.select_related("customer", "region", "sales_page").prefetch_related("lines__product", "order_offers").get(pk=int(create_exchange_id))
            # Only allow exchanges from delivered or paid orders
            if src.status in (Order.LIVREE, Order.PAYEE):
                exchange_source = src
        except (Order.DoesNotExist, ValueError):
            pass

    return render(request, "inventory/orders_list.html", {
        "orders": orders,
        "status_filter": status_filter,
        "source_filter": source_filter,
        "today_on": today_on,
        "counts": counts,
        "total": source_total,
        "future_count": future_count,
        "show_scheduled": request.GET.get("show_scheduled") == "1",
        # Data needed by the inline-create row + modal
        "sales_pages": SalesPage.objects.filter(is_active=True),
        "regions": Region.objects.filter(is_active=True),
        # Exchange source for pre-filling the editor
        "exchange_source": exchange_source,
    })


@login_required(login_url="/login/")
def order_create(request):
    """Legacy alias — redirects to orders list (inline-create lives there)."""
    return redirect("orders_list")


# ---- New inline-create flow APIs ------------------------------------------

@login_required(login_url="/login/")
def api_offers_for_page(request, page_id):
    """Return active offers attached to a given SalesPage."""
    if not _orders_role_check(request):
        return JsonResponse({"status": "error"}, status=403)
    from .models import Offer, SalesPage
    page = SalesPage.objects.filter(id=page_id).first()
    offers = Offer.objects.filter(is_active=True, sales_pages__id=page_id).distinct()
    return JsonResponse({
        "status": "ok",
        "offers": [
            {"id": o.id, "name": o.name, "bundle_price": str(o.price_for_page(page))}
            for o in offers
        ],
    })


@login_required(login_url="/login/")
def api_all_offers(request):
    """Return ALL active offers across every page, with the page name(s) so the
    user can pick an offer from another page in the order form."""
    if not _orders_role_check(request):
        return JsonResponse({"status": "error"}, status=403)
    from .models import Offer
    offers = Offer.objects.filter(is_active=True).prefetch_related("sales_pages").distinct().order_by("name")
    data = []
    for o in offers:
        page_names = ", ".join(p.name for p in o.sales_pages.all()) or "—"
        data.append({
            "id": o.id, "name": o.name, "bundle_price": str(o.bundle_price),
            "page": page_names,
        })
    return JsonResponse({"status": "ok", "offers": data})


@login_required(login_url="/login/")
def api_offer_detail(request, offer_id):
    """Return an offer's products with each product's variants and sizes-with-stock."""
    if not _orders_role_check(request):
        return JsonResponse({"status": "error"}, status=403)
    from .models import Offer
    try:
        offer = Offer.objects.prefetch_related("products__product__variants__units").get(pk=offer_id)
    except Offer.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Offre introuvable."}, status=404)

    products_data = []
    for op in offer.products.all():
        product = op.product

        # Build the "family" of products that share the same physical SKU.
        # Whichever product the user picked when creating the offer (parent OR
        # V2 OR V3), find the root (parent_product OR self) then list all
        # products in the family.
        from .models import Product
        from django.db.models import Q
        root = product.parent_product or product
        family = Product.objects.filter(
            Q(id=root.id) | Q(parent_product=root)
        ).prefetch_related("variants__units")

        # Group variants by color across the whole family so V2 "noir" merges
        # with V1 "noir" — same physical color = one pick for the office user.
        from collections import OrderedDict
        by_color = OrderedDict()
        for fam_p in family:
            for v in fam_p.variants.all():
                color_key = (v.color_label or v.color_name or "").strip().lower() or f"var{v.id}"
                if color_key not in by_color:
                    by_color[color_key] = {
                        "id": v.id,  # representative variant id
                        # A single-colour product may have a blank colour; show
                        # "Unique" rather than an empty "—" so it's selectable.
                        "color": (v.color_label or v.color_name or "").strip() or "Unique",
                        "image_url": v.image.url if v.image else None,
                        "sizes": [],
                        "stock_by_size": {},
                        "variant_ids": [],  # all variant ids merged under this color
                    }
                entry = by_color[color_key]
                entry["variant_ids"].append(v.id)
                if not entry["image_url"] and v.image:
                    entry["image_url"] = v.image.url
                for u in v.units.all():
                    if u.size and u.size not in entry["sizes"]:
                        entry["sizes"].append(u.size)
                    if u.status in (ProductUnit.IN_STOCK, ProductUnit.RETURNED):
                        entry["stock_by_size"][u.size] = entry["stock_by_size"].get(u.size, 0) + 1

        variants = list(by_color.values())

        products_data.append({
            "offer_product_id": op.id,
            "product_id": product.id,
            "product_name": product.name,
            "quantity": op.quantity,
            "variants": variants,
        })

    # Resolve the price for the page this order is on (per-page pricing). Falls
    # back to the default bundle_price when no page is given or no override.
    page_id = request.GET.get("page")
    page_name = request.GET.get("page_name")
    resolved_price = offer.bundle_price
    if page_id:
        try:
            from .models import SalesPage
            sp = SalesPage.objects.filter(pk=int(page_id)).first()
            if sp is not None:
                resolved_price = offer.price_for_page(sp)
        except (ValueError, TypeError):
            pass
    elif page_name:
        resolved_price = offer.price_for_page_name(page_name)

    return JsonResponse({
        "status": "ok",
        "offer": {
            "id": offer.id, "name": offer.name,
            "bundle_price": str(resolved_price),
            "default_price": str(offer.bundle_price),
            "is_active": offer.is_active,
            "sales_page_ids": list(offer.sales_pages.values_list("id", flat=True)),
            "page_prices": {str(pp.sales_page_id): str(pp.price) for pp in offer.page_prices.all()},
            "products": products_data,
        },
    })


@csrf_exempt
@require_POST
@login_required(login_url="/login/")
def api_create_order_inline(request):
    """Save a new order from the inline-row form.

    Expected JSON payload:
    {
      "phone": "...", "name": "...",
      "sales_page": <id>, "region": <id>,
      "ville": "...", "localite": "...", "address": "...",
      "delivery_fee": 7, "discount": 0, "notes": "...",
      "offers": [
        {
          "offer_id": 12, "quantity": 1,
          "products": [
            {"offer_product_id": 5, "product_id": 3, "variant_id": 8, "size": "M", "quantity": 1},
            ...
          ]
        }, ...
      ]
    }
    """
    from .models import (
        Order, OrderLine, OrderOffer, Offer, Customer, SalesPage, Region,
        log_action, AuditLog,
    )
    if not _orders_role_check(request):
        return JsonResponse({"status": "error", "message": "Accès refusé."}, status=403)
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "JSON invalide."}, status=400)

    try:
        phone = (data.get("phone") or "").strip()
        if not phone:
            return JsonResponse({"status": "error", "message": "Téléphone obligatoire."}, status=400)

        sales_page_id = data.get("sales_page")
        if not sales_page_id:
            return JsonResponse({"status": "error", "message": "Page obligatoire."}, status=400)

        offers_payload = data.get("offers", [])
        if not offers_payload:
            return JsonResponse({"status": "error", "message": "Au moins une offre requise."}, status=400)

        name = (data.get("name") or "").strip()
        customer, _ = Customer.objects.get_or_create(phone=phone, defaults={"name": name})
        if name and customer.name != name:
            customer.name = name
            customer.save(update_fields=["name"])

        delivery_fee = Decimal(str(data.get("delivery_fee") or "7"))
        discount = Decimal(str(data.get("discount") or "0"))

        with transaction.atomic():
            order = Order.objects.create(
                customer=customer,
                sales_page_id=sales_page_id,
                region_id=data.get("region") or None,
                ville=(data.get("ville") or "").strip(),
                localite=(data.get("localite") or "").strip(),
                address=(data.get("address") or "").strip(),
                delivery_fee=delivery_fee,
                discount=discount,
                notes=(data.get("notes") or "").strip(),
                created_by=request.user if request.user.is_authenticated else None,
            )

            for op in offers_payload:
                try:
                    offer = Offer.objects.get(pk=op.get("offer_id"))
                except Offer.DoesNotExist:
                    continue
                qty = max(int(op.get("quantity") or 1), 1)
                order_offer = OrderOffer.objects.create(
                    order=order, offer=offer,
                    offer_name=offer.name,
                    bundle_price=offer.price_for_page(order.sales_page),
                    quantity=qty,
                )
                for line in op.get("products", []):
                    OrderLine.objects.create(
                        order=order,
                        order_offer=order_offer,
                        product_id=line.get("product_id"),
                        variant_id=line.get("variant_id") or None,
                        size=(line.get("size") or "").strip(),
                        quantity=max(int(line.get("quantity") or 1), 1),
                        unit_price=0,  # individual product price not used inside an offer
                    )

            order.recalc_total()

        log_action(
            request.user, AuditLog.CREATE,
            description=f"Commande #{order.id} créée pour {customer} ({order.order_offers.count()} offre(s), {order.total} TND)",
            request=request,
            target_model="Order", target_id=order.id,
        )
        return JsonResponse({
            "status": "ok",
            "order": {
                "id": order.id,
                "total": str(order.total),
                "redirect": f"/orders/{order.id}/",
            },
        })
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)}, status=400)


# ---- AUTO-SAVE / DRAFT FLOW (Phase A) -------------------------------------
# These let the frontend save the order progressively as the user fills it in,
# instead of waiting for a "Save" button. The order is created as soon as the
# phone number is filled in; from then on every field change → autosave call.
# All edits are refused once status != non_confirmee (server-enforced lock).

def _is_draft_editable(order):
    """An order stays editable through the early call-center statuses
    (non confirmée, injoignable, pas sérieux, rappeler, annulée…).
    It locks only once it is CONFIRMÉE or LIVRÉE, or once it has been pushed
    to Navex (has a bordereau barcode)."""
    LOCKED_STATUSES = {"confirmee", "livree", "au_magasin", "en_cours"}
    if order.status in LOCKED_STATUSES:
        return False
    if order.bordereau_barcode:
        return False
    return True


@csrf_exempt
@require_POST
@login_required(login_url="/login/")
def api_order_draft_discard(request, pk):
    """Discard (delete) a draft order — used when the operator abandons an order
    they started entering (e.g. realised it's a duplicate). Only allowed while
    the order is still an editable draft (not confirmed/pushed to Navex)."""
    from .models import Order, log_action, AuditLog
    if not _orders_role_check(request):
        return JsonResponse({"status": "error", "message": "Accès refusé."}, status=403)
    try:
        order = Order.objects.get(pk=pk)
    except Order.DoesNotExist:
        # Already gone — treat as success so the UI cleans up.
        return JsonResponse({"status": "ok", "deleted": False})
    if not _is_draft_editable(order):
        return JsonResponse({
            "status": "error",
            "message": "Cette commande n'est plus un brouillon — suppression impossible.",
            "locked": True,
        }, status=400)
    oid = order.id
    # Clean up any attached lines/offers first.
    try:
        order.lines.all().delete()
        order.order_offers.all().delete()
    except Exception:
        pass
    order.delete()
    try:
        log_action(
            request.user, AuditLog.DELETE,
            description=f"Brouillon commande #{oid} supprimé (abandonné)",
            request=request, target_model="Order", target_id=oid,
        )
    except Exception:
        pass
    return JsonResponse({"status": "ok", "deleted": True})


@csrf_exempt
@require_POST
@login_required(login_url="/login/")
def api_order_draft_upsert(request):
    """Create or update a draft order.
    - If 'order_id' is in payload → update existing draft (with edit-lock check)
    - Otherwise → create new draft (requires phone + sales_page)

    Accepts ANY subset of fields. Unspecified fields aren't touched.
    Returns {status:'ok', order_id, status, total, locked: bool}.
    """
    from .models import (
        Order, OrderLine, OrderOffer, Offer, Customer, SalesPage, Region,
        log_action, AuditLog,
    )
    if not _orders_role_check(request):
        return JsonResponse({"status": "error", "message": "Accès refusé."}, status=403)
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "JSON invalide."}, status=400)

    order_id = data.get("order_id")
    # Normalize phone: keep only digits (must match frontend cleanPhone()).
    # This way "12 345 678", "12.345.678", " 12345678 " all become "12345678".
    raw_phone = (data.get("phone") or "")
    phone = "".join(ch for ch in raw_phone if ch.isdigit())
    raw_phone2 = (data.get("phone2") or "")
    phone2 = "".join(ch for ch in raw_phone2 if ch.isdigit())
    name = (data.get("name") or "").strip()

    try:
        if order_id:
            # UPDATE existing draft
            try:
                order = Order.objects.select_related("customer").get(pk=order_id)
            except Order.DoesNotExist:
                return JsonResponse({"status": "error", "message": "Brouillon introuvable."}, status=404)
            if not _is_draft_editable(order):
                return JsonResponse({
                    "status": "error",
                    "message": "Cette commande n'est plus modifiable (déjà confirmée).",
                    "locked": True,
                }, status=400)
        else:
            # CREATE new draft — require phone (8 digits, Tunisian) + sales_page
            # Return 200 with "waiting" status instead of 400 so the frontend
            # doesn't spam errors before the user has finished typing.
            if not phone:
                return JsonResponse({"status": "waiting", "message": "Téléphone requis pour créer le brouillon."})
            if len(phone) != 8:
                return JsonResponse({"status": "waiting", "message": f"Téléphone invalide ({len(phone)} chiffres, 8 requis)."})
            sales_page_id = data.get("sales_page")
            if not sales_page_id:
                return JsonResponse({"status": "waiting", "message": "Page requise pour créer le brouillon."})
            # Normalize phone — strip whitespace, remove any hidden chars
            # to avoid creating a "duplicate" customer with phone=" 11111111".
            phone_norm = phone.strip()
            customer = Customer.objects.filter(phone=phone_norm).first()
            if customer is None:
                # Try to create. If a race condition or normalization issue causes
                # IntegrityError, fall back to the existing one.
                try:
                    customer = Customer.objects.create(phone=phone_norm, name=name)
                except Exception:
                    customer = Customer.objects.filter(phone=phone_norm).first()
                    if customer is None:
                        return JsonResponse({"status": "error", "message": f"Impossible de créer/trouver le client {phone_norm}."}, status=400)
            if name and customer.name != name:
                customer.name = name
                customer.save(update_fields=["name"])
            if phone2 and customer.phone2 != phone2:
                customer.phone2 = phone2
                customer.save(update_fields=["phone2"])
            # EXCHANGE de-dup: if this is an exchange draft and an OPEN (still
            # editable, not pushed) exchange draft already exists for the same
            # source order + customer, reuse it instead of creating a duplicate.
            # Wrapped in a transaction with row-locking on the source order so
            # near-simultaneous autosaves (which fire before the frontend knows
            # the draft id) serialize and only ONE draft is ever created.
            exchange_of_id = data.get("exchange_of_id")
            order = None
            if exchange_of_id:
                try:
                    src_id = int(exchange_of_id)
                    with transaction.atomic():
                        # Lock the source order row so concurrent exchange-draft
                        # creates for the same source line up one at a time.
                        try:
                            Order.objects.select_for_update().filter(pk=src_id).first()
                        except Exception:
                            pass
                        existing = (Order.objects
                                    .filter(exchange_of_id=src_id,
                                            customer=customer,
                                            bordereau_barcode="")
                                    .exclude(status__in=["confirmee", "livree", "payee",
                                                          "au_magasin", "en_cours"])
                                    .order_by("-created_at")
                                    .first())
                        if existing is not None:
                            order = existing
                        else:
                            order = Order.objects.create(
                                customer=customer,
                                sales_page_id=sales_page_id,
                                customer_name=name,
                                created_by=request.user if request.user.is_authenticated else None,
                                status="non_confirmee",
                                exchange_of_id=src_id,
                            )
                except (ValueError, TypeError):
                    order = None
            if order is None:
                order = Order.objects.create(
                    customer=customer,
                    sales_page_id=sales_page_id,
                    customer_name=name,
                    created_by=request.user if request.user.is_authenticated else None,
                    status="non_confirmee",
                )
            # If this is an exchange (frontend passes exchange_of_id), link the
            # new order to the original delivered order.
            if exchange_of_id and not order.exchange_of_id:
                try:
                    src = Order.objects.get(pk=int(exchange_of_id))
                    if src.status in (Order.LIVREE, Order.PAYEE):
                        order.exchange_of_id = src.id
                        order.save(update_fields=["exchange_of"])
                except (Order.DoesNotExist, ValueError, TypeError):
                    pass
            log_action(
                request.user, AuditLog.CREATE,
                description=f"Brouillon créé (auto) — {phone_norm}" + (f" [échange de #{exchange_of_id}]" if exchange_of_id else ""),
                request=request, target_model="Order", target_id=order.id,
            )

        # Apply any provided fields
        changed = []
        # Customer fields. IMPORTANT: phone is a property of the Customer, and
        # several orders can share one Customer. So changing the phone must NOT
        # rewrite the shared Customer (that would change every other order of
        # that person). Instead, re-link THIS order to the Customer matching the
        # new phone (creating it if needed), leaving the others untouched.
        if phone and order.customer and order.customer.phone != phone:
            new_customer, _created = Customer.objects.get_or_create(
                phone=phone,
                defaults={"name": name or order.customer.name or ""},
            )
            order.customer = new_customer
            order.save(update_fields=["customer"])
            changed.append("phone")
        # Name is per-order (same person/phone can order under different names).
        if name and order.customer_name != name:
            order.customer_name = name
            order.save(update_fields=["customer_name"])
            changed.append("name")
        # Phone2 is optional — only update if the payload includes it (so blank
        # payload doesn't accidentally wipe an existing secondary number)
        if "phone2" in data and order.customer:
            new_phone2 = phone2  # already cleaned to digits-only above
            if order.customer.phone2 != new_phone2:
                order.customer.phone2 = new_phone2
                order.customer.save(update_fields=["phone2"])
                changed.append("phone2")
        # Direct order fields — only update if present in payload
        if "sales_page" in data and data["sales_page"]:
            order.sales_page_id = data["sales_page"]
            changed.append("sales_page")
        if "region" in data:
            order.region_id = data["region"] or None
            changed.append("region")
        if "ville" in data:
            order.ville = (data["ville"] or "").strip()
            changed.append("ville")
        if "localite" in data:
            order.localite = (data["localite"] or "").strip()
            changed.append("localite")
        if "address" in data:
            order.address = (data["address"] or "").strip()
            changed.append("address")
        if "delivery_fee" in data:
            try:
                order.delivery_fee = Decimal(str(data["delivery_fee"]))
                changed.append("delivery_fee")
            except Exception:
                pass
        # Phase 2.2: exchange fault toggle. "ours" → free shipping (0 DT);
        # "client" → standard 7 DT. Setting the fault auto-adjusts delivery_fee
        # unless the payload also explicitly set delivery_fee in the same call.
        if "exchange_fault" in data:
            fault = (data.get("exchange_fault") or "").strip()
            valid_faults = dict(Order.EXCHANGE_FAULT_CHOICES)
            if fault in valid_faults:
                order.exchange_fault = fault
                changed.append("exchange_fault")
                if "delivery_fee" not in data:
                    if fault == Order.EXCHANGE_FAULT_OURS:
                        order.delivery_fee = Decimal("0")
                        changed.append("delivery_fee")
                    elif fault == Order.EXCHANGE_FAULT_CLIENT:
                        order.delivery_fee = Decimal("7")
                        changed.append("delivery_fee")
        if "discount" in data:
            try:
                order.discount = Decimal(str(data["discount"]))
                changed.append("discount")
            except Exception:
                pass
        if "notes" in data:
            order.notes = (data["notes"] or "").strip()
            changed.append("notes")
        if changed:
            order.save()

        # Replace offers if the payload contains them (full replace, not partial)
        if "offers" in data:
            with transaction.atomic():
                # Wipe existing lines and offers, then rebuild from payload
                order.lines.all().delete()
                order.order_offers.all().delete()
                for op in data.get("offers", []):
                    offer_id = op.get("offer_id")
                    if not offer_id:
                        continue
                    try:
                        offer = Offer.objects.get(pk=offer_id)
                    except Offer.DoesNotExist:
                        continue
                    qty = max(int(op.get("quantity") or 1), 1)
                    order_offer = OrderOffer.objects.create(
                        order=order, offer=offer,
                        offer_name=offer.name,
                        bundle_price=offer.price_for_page(order.sales_page),
                        quantity=qty,
                    )
                    for line in op.get("products", []):
                        OrderLine.objects.create(
                            order=order,
                            order_offer=order_offer,
                            product_id=line.get("product_id"),
                            variant_id=line.get("variant_id") or None,
                            size=(line.get("size") or "").strip(),
                            quantity=max(int(line.get("quantity") or 1), 1),
                            unit_price=0,  # not used inside an offer (offer.bundle_price drives the total)
                        )
            changed.append("offers")

        # CRITICAL: recompute the order total after any change.
        # Without this, Order.total stays at 0 and we'd push prix=0 to Navex.
        order.recalc_total()

        return JsonResponse({
            "status": "ok",
            "order_id": order.id,
            "order_status": order.status,
            "total": str(order.total),
            "locked": not _is_draft_editable(order),
            "changed": changed,
        })
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)}, status=400)


@login_required(login_url="/login/")
def api_order_draft_get(request, pk):
    """Return the full state of an order as JSON, for re-opening the edit form."""
    from .models import Order
    if not _orders_role_check(request):
        return JsonResponse({"status": "error"}, status=403)
    try:
        order = Order.objects.select_related("customer", "sales_page", "region").prefetch_related(
            "order_offers", "lines"
        ).get(pk=pk)
    except Order.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Commande introuvable."}, status=404)

    offers_data = []
    for oo in order.order_offers.all():
        # Group lines belonging to this offer
        products = []
        for line in order.lines.filter(order_offer=oo):
            products.append({
                "offer_product_id": None,
                "product_id": line.product_id,
                "variant_id": line.variant_id,
                "size": line.size,
                "quantity": line.quantity,
            })
        offers_data.append({
            "offer_id": oo.offer_id,
            "quantity": oo.quantity,
            "products": products,
        })

    # Standalone product lines (no order_offer) — typically from Shopify webhook
    # when a single product (not a bundle) was purchased.
    standalone_lines = []
    for line in order.lines.filter(order_offer__isnull=True):
        standalone_lines.append({
            "product_id": line.product_id,
            "variant_id": line.variant_id,
            "size": line.size,
            "quantity": line.quantity,
            "unit_price": str(line.unit_price) if line.unit_price else "0",
        })

    return JsonResponse({
        "status": "ok",
        "order": {
            "id": order.id,
            "status": order.status,
            "status_display": order.get_status_display(),
            "locked": not _is_draft_editable(order),
            "phone": order.customer.phone if order.customer else "",
            "phone2": order.customer.phone2 if order.customer else "",
            "name": order.display_name if order.customer else "",
            "sales_page": order.sales_page_id,
            "sales_page_name": order.sales_page.name if order.sales_page else "",
            "region": order.region_id,
            "region_name": order.region.name if order.region else "",
            "ville": order.ville,
            "localite": order.localite,
            "address": order.address,
            "delivery_fee": str(order.delivery_fee),
            "discount": str(order.discount),
            "notes": order.notes,
            "offers": offers_data,
            "standalone_lines": standalone_lines,
            "total": str(order.total),
            "article_summary": order.article_summary,
            "created_at": order.created_at.strftime("%d/%m %H:%M") if order.created_at else "",
            "exchange_of_id": order.exchange_of_id,
            "return_items_count": order.return_items.count() if order.exchange_of_id else 0,
        },
    })


# ---- Search API ------------------------------------------------------------

@login_required(login_url="/login/")
def api_orders_search(request):
    """Search orders across multiple fields. Returns up to 200 matches.

    Search logic:
      - If query starts with "#" → match exact order ID
      - If query is all digits → match phone, phone2, bordereau, or ID
      - Otherwise (text) → match customer name (case-insensitive contains)
    """
    from .models import Order
    if not _orders_role_check(request):
        return JsonResponse({"status": "error"}, status=403)

    q = (request.GET.get("q") or "").strip()
    if not q:
        return JsonResponse({"status": "ok", "results": []})

    from django.db.models import Q

    qs = Order.objects.select_related("customer", "region", "sales_page")

    if q.startswith("#"):
        try:
            order_id = int(q[1:])
            qs = qs.filter(pk=order_id)
        except ValueError:
            return JsonResponse({"status": "ok", "results": []})
    elif q.isdigit():
        try:
            order_id_match = int(q)
        except ValueError:
            order_id_match = None
        f = Q(customer__phone__icontains=q) | Q(customer__phone2__icontains=q) | Q(bordereau_barcode__icontains=q)
        if order_id_match is not None:
            f |= Q(pk=order_id_match)
        qs = qs.filter(f)
    else:
        qs = qs.filter(Q(customer_name__icontains=q) | Q(customer__name__icontains=q))

    qs = qs.order_by("-created_at")[:200]
    # Count total orders per customer (for the repeat-customer badge), plus
    # combined delivered/returned including historic stats.
    from django.db.models import Count as _Count2
    cust_ids = {o.customer_id for o in qs if o.customer_id}
    pcounts = {}
    deliv = {}
    retd = {}
    if cust_ids:
        for row in (Order.objects.filter(customer_id__in=cust_ids)
                    .values("customer_id").annotate(n=_Count2("id"))):
            pcounts[row["customer_id"]] = row["n"]
        for row in (Order.objects.filter(customer_id__in=cust_ids, status__in=["livree", "payee"])
                    .values("customer_id").annotate(n=_Count2("id"))):
            deliv[row["customer_id"]] = row["n"]
        for row in (Order.objects.filter(customer_id__in=cust_ids, status="returned")
                    .values("customer_id").annotate(n=_Count2("id"))):
            retd[row["customer_id"]] = row["n"]
    from .models import Customer as _Cust2, CustomerHistory as _CH2
    phones2 = dict(_Cust2.objects.filter(id__in=cust_ids).values_list("id", "phone"))
    hist2 = {}
    try:
        hist2 = {h.phone: h for h in _CH2.objects.filter(phone__in=phones2.values())}
    except Exception:
        hist2 = {}
    results = []
    for o in qs:
        ph2 = phones2.get(o.customer_id)
        h2 = hist2.get(ph2) if ph2 else None
        h_total = h2.historic_total if h2 else 0
        h_deliv = h2.historic_delivered if h2 else 0
        h_ret = h2.historic_returned if h2 else 0
        results.append({
            "id": o.id,
            "phone": o.customer.phone if o.customer else "",
            "phone2": o.customer.phone2 if o.customer else "",
            "phone_order_count": pcounts.get(o.customer_id, 1) + h_total,
            "combined_delivered": deliv.get(o.customer_id, 0) + h_deliv,
            "combined_returned": retd.get(o.customer_id, 0) + h_ret,
            "is_vip": (deliv.get(o.customer_id, 0) + h_deliv) >= 3,
            "is_risky": (retd.get(o.customer_id, 0) + h_ret) > (deliv.get(o.customer_id, 0) + h_deliv) and (retd.get(o.customer_id, 0) + h_ret) > 0,
            "name": o.customer.name if o.customer else "",
            "status": o.status,
            "status_display": o.get_status_display(),
            "sales_page_name": o.sales_page.name if o.sales_page else "",
            "region_name": o.region.name if o.region else "",
            "ville": o.ville,
            "address": o.address or "",
            "total": str(o.total),
            "amount_collected": str(o.amount_collected) if o.amount_collected is not None else None,
            "status_note": o.status_note or "",
            "status_note_at": o.status_note_at.strftime("%d/%m %H:%M") if o.status_note_at else "",
            "bordereau": o.bordereau_barcode,
            "navex_last_status": o.navex_last_status or "",
            "navex_last_synced_at": o.navex_last_synced_at.strftime("%d/%m %H:%M") if o.navex_last_synced_at else "",
            "article_summary": o.article_summary,
            "created_at": o.created_at.strftime("%d/%m %H:%M") if o.created_at else "",
            "scheduled_for": o.scheduled_for.strftime("%Y-%m-%d") if o.scheduled_for else "",
            "is_exchange": bool(o.exchange_of_id),
        })
    return JsonResponse({"status": "ok", "results": results, "count": len(results)})


# ---- Schedule an order for later --------------------------------------------

@csrf_exempt
@require_POST
@login_required(login_url="/login/")
def api_order_set_scheduled(request, pk):
    """Update the scheduled_for date on an order.
    Body: {scheduled_for: "YYYY-MM-DD"} or {scheduled_for: ""} to clear.
    """
    from .models import Order, log_action, AuditLog
    if not _orders_role_check(request):
        return JsonResponse({"status": "error"}, status=403)
    try:
        order = Order.objects.get(pk=pk)
    except Order.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Commande introuvable."}, status=404)
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "JSON invalide."}, status=400)
    raw = (data.get("scheduled_for") or "").strip()
    from datetime import datetime, date as _date
    new_date = None
    if raw:
        try:
            new_date = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            return JsonResponse({"status": "error", "message": "Format de date invalide (attendu YYYY-MM-DD)."}, status=400)
    old_date = order.scheduled_for
    order.scheduled_for = new_date
    order.save(update_fields=["scheduled_for", "updated_at"])
    log_action(
        request.user, AuditLog.EDIT,
        description=f"Date prévue changée : {old_date} → {new_date} pour commande #{order.id}",
        target_model="Order", target_id=order.id,
    )
    return JsonResponse({
        "status": "ok",
        "scheduled_for": new_date.strftime("%Y-%m-%d") if new_date else "",
        "display": new_date.strftime("%d/%m") if new_date else "—",
    })


# ---- Ads dashboard: spend (from Meta, per campaign) linked to offers --------

def _meta_fetch_spend_by_campaign(start_date, end_date):
    """Fetch ad spend PER CAMPAIGN from Meta between two dates (inclusive).
    Returns a dict {campaign_id: {"name": str, "spend": float}}. Empty on error.

    Keyed on campaign_id (STABLE) so renaming a campaign in Ads Manager doesn't
    create a duplicate. Spend from multiple rows of the same campaign is summed.

    Env vars: META_ACCESS_TOKEN and META_AD_ACCOUNT_ID.
    """
    import urllib.request
    token = os.environ.get("META_ACCESS_TOKEN", "").strip()
    accounts_raw = os.environ.get("META_AD_ACCOUNT_ID", "").strip()
    if not accounts_raw:
        return {}
    # META_AD_ACCOUNT_ID may be a single id or a comma-separated list of ids,
    # so campaigns from several ad accounts (e.g. Barats + Converty) all sync.
    account_ids = [a.strip() for a in accounts_raw.split(",") if a.strip()]
    # Optional per-account tokens (when accounts live in different portfolios
    # and no single token can read them all). Format:
    #   META_AD_ACCOUNT_TOKENS = "1465...:EAA...,1865...:EAB..."
    # Any account not listed here falls back to META_ACCESS_TOKEN.
    per_account = {}
    for pair in os.environ.get("META_AD_ACCOUNT_TOKENS", "").split(","):
        pair = pair.strip()
        if pair and ":" in pair:
            aid, _, tok = pair.partition(":")
            per_account[aid.strip().replace("act_", "")] = tok.strip()
    if not token and not per_account:
        return {}
    # Per-account currency conversion to TND (dinars). Ad accounts can be in
    # different currencies (e.g. Ibrahim=EUR, Converty=USD) while our system and
    # revenue are in TND, so raw spend can't be summed directly. Configure per
    # account via META_ACCOUNT_RATES = "accountid:CURRENCY:rate,..." where rate =
    # how many TND for 1 unit of that currency. Back-compat: "accountid:rate"
    # (no currency) still works. Any account not listed defaults to rate 1.0.
    account_rates = {}      # bare_id -> float rate
    account_currency = {}   # bare_id -> "EUR"/"USD"/...
    for pair in os.environ.get("META_ACCOUNT_RATES", "").split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        parts = [p.strip() for p in pair.split(":")]
        aid = parts[0].replace("act_", "")
        if len(parts) >= 3:
            cur, rate = parts[1], parts[2]
        else:
            cur, rate = "", parts[1]
        try:
            account_rates[aid] = float(rate)
        except (ValueError, TypeError):
            continue
        if cur:
            account_currency[aid] = cur.upper()
    result = {}
    for account_id in account_ids:
        bare = account_id.replace("act_", "")
        acc_token = per_account.get(bare, token)
        if not acc_token:
            continue
        rate = account_rates.get(bare, 1.0)  # TND per 1 unit of account currency
        cur = account_currency.get(bare, "TND")
        acct = account_id if account_id.startswith("act_") else f"act_{account_id}"
        url = (
            f"https://graph.facebook.com/v18.0/{acct}/insights"
            f"?level=campaign&fields=campaign_id,campaign_name,spend"
            f"&time_range={{'since':'{start_date}','until':'{end_date}'}}"
            f"&limit=500&access_token={acc_token}"
        )
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception:
            continue  # one account failing shouldn't block the others
        for entry in data.get("data", []):
            cid = entry.get("campaign_id", "")
            name = entry.get("campaign_name", "") or cid
            if not cid:
                continue
            try:
                orig = float(entry.get("spend", "0"))
            except (ValueError, TypeError):
                orig = 0.0
            tnd = orig * rate
            if cid in result:
                result[cid]["spend"] += tnd
                result[cid]["spend_original"] += orig
                result[cid]["name"] = name
            else:
                result[cid] = {
                    "name": name, "spend": tnd, "spend_original": orig,
                    "currency": cur, "account_id": bare,
                }
    return result


def _meta_fetch_campaign_status():
    """Fetch effective_status per campaign_id across all configured ad accounts.
    Returns {campaign_id: "ACTIVE"/"PAUSED"/"CAMPAIGN_PAUSED"/"DELETED"/...}.
    Uses the /campaigns endpoint (all campaigns, not just those with spend)."""
    import urllib.request
    token = os.environ.get("META_ACCESS_TOKEN", "").strip()
    accounts_raw = os.environ.get("META_AD_ACCOUNT_ID", "").strip()
    if not accounts_raw:
        return {}
    account_ids = [a.strip() for a in accounts_raw.split(",") if a.strip()]
    per_account = {}
    for pair in os.environ.get("META_AD_ACCOUNT_TOKENS", "").split(","):
        pair = pair.strip()
        if pair and ":" in pair:
            aid, _, tok = pair.partition(":")
            per_account[aid.strip().replace("act_", "")] = tok.strip()
    result = {}
    for account_id in account_ids:
        bare = account_id.replace("act_", "")
        acc_token = per_account.get(bare, token)
        if not acc_token:
            continue
        acct = account_id if account_id.startswith("act_") else f"act_{account_id}"
        url = (f"https://graph.facebook.com/v18.0/{acct}/campaigns"
               f"?fields=id,effective_status&limit=500&access_token={acc_token}")
        while url:
            try:
                with urllib.request.urlopen(url, timeout=20) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
            except Exception:
                break
            for c in data.get("data", []):
                cid = c.get("id")
                if cid:
                    result[cid] = c.get("effective_status", "")
            url = data.get("paging", {}).get("next")
    return result


def _sync_ads_from_meta(start_date, end_date):
    """Upsert Ad rows from Meta per-campaign spend, keyed on campaign_id.
    Also refreshes the display name and migrates any legacy name-only rows.
    Returns (ok, message, n)."""
    from decimal import Decimal
    from django.utils import timezone
    from .models import Ad
    spend_map = _meta_fetch_spend_by_campaign(start_date, end_date)
    if not spend_map:
        return False, "Aucune donnée Meta (vérifier le token / la période).", 0
    now = timezone.now()
    n = 0
    for cid, info in spend_map.items():
        name = info["name"]
        spend = info["spend"]
        ad = Ad.objects.filter(campaign_id=cid).first()
        if ad is None:
            # Legacy row synced by name before campaign_id existed? Adopt it so
            # its offer links are preserved, then stamp the id.
            ad = Ad.objects.filter(campaign_id__isnull=True, campaign_name=name).first()
            if ad is None:
                ad = Ad(campaign_id=cid, campaign_name=name)
            else:
                ad.campaign_id = cid
        ad.campaign_name = name  # refresh display name (handles renames)
        ad.spend = Decimal(str(round(spend, 2)))
        ad.spend_original = Decimal(str(round(info.get("spend_original", 0.0), 2)))
        ad.currency = info.get("currency", "") or ad.currency
        ad.account_id = info.get("account_id", "") or ad.account_id
        ad.last_synced_at = now
        ad.save()
        n += 1

    # Refresh effective_status for ALL known ads (not just those with spend
    # today), so cancelled/paused campaigns get flagged even when idle.
    status_map = _meta_fetch_campaign_status()
    if status_map:
        for ad in Ad.objects.exclude(campaign_id__isnull=True):
            st = status_map.get(ad.campaign_id)
            if st is not None and st != ad.effective_status:
                ad.effective_status = st
                ad.save(update_fields=["effective_status"])
    return True, f"{n} publicité(s) synchronisée(s) depuis Meta.", n


@login_required(login_url="/login/")
def ads_offers_dashboard(request):
    """Ads / cost-per-order dashboard over a date range (default: today).

    Two attribution models:

    * BARATS.TN CARROUSEL (ad.attribution == 'barats'): every barats.tn ad is
      one carousel for the whole site. We can't attribute spend to a single
      offer, so we POOL: sum all barats-ad spend and divide by ALL qualifying
      orders whose sales_page is Barats.tn in the period. => one blended
      cost-per-order for the website.

    * OFFER-LINKED (ad.attribution == 'offer'): each Converty/Facebook ad is
      linked to 1 or 2 offers. Spend is pooled across that ad's linked offers:
      count all qualifying orders that contain ANY of the linked offers, then
      cost-per-order = ad.spend / that count. (E.g. 10$ ad linked to offer A
      (3 orders) + offer B (7 orders) => 10 orders => 1$/order.)

    Qualifying order statuses = En cours (expédié) + Livrée + Payée. On today's
    date you'll mostly see 'en_cours'; livrée/payée fill in over the next days.
    """
    if not _orders_role_check(request):
        return redirect("home")
    from datetime import date, datetime
    from collections import defaultdict
    from .models import Ad, Offer, Order, SalesPage, ShippingOrder

    QUALIFYING = [Order.EN_COURS, Order.AU_MAGASIN, Order.LIVREE, Order.PAYEE]

    today = date.today()
    # Date range. Default: today only. ?start=YYYY-MM-DD&end=YYYY-MM-DD
    # Back-compat: ?day=YYYY-MM-DD sets both start and end to that day.
    day_param = request.GET.get("day", "")
    try:
        start = datetime.strptime(request.GET.get("start", "") or day_param, "%Y-%m-%d").date()
    except ValueError:
        start = today
    try:
        end = datetime.strptime(request.GET.get("end", "") or day_param, "%Y-%m-%d").date()
    except ValueError:
        end = today
    if end < start:
        start, end = end, start
    start_str, end_str = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

    # Sync spend for the whole range from Meta.
    _sync_ads_from_meta(start_str, end_str)

    # Archived ads (cancelled/disabled in Meta) are hidden and excluded from
    # attribution — they no longer capture orders. Their past-date data still
    # shows because on those dates they weren't archived… (archive is a current
    # flag; simplest correct behaviour: once archived, drop from all views).
    ads = list(Ad.objects.filter(archived=False)
               .prefetch_related("links__offer", "links__sales_page").all())
    offers = list(Offer.objects.filter(is_active=True).order_by("name"))
    pages = list(SalesPage.objects.filter(is_active=True).order_by("name"))

    # Map each (offer_id, page_id) -> ad, and offer_id -> ad (any page), so an
    # order can be attributed to the FIRST linked ad found. Used for real-margin
    # profit: the whole order's net (total − delivery) goes to one ad, casquette
    # and all, since that ad's campaign brought the customer.
    offer_page_to_ad = {}   # (offer_id, page_id) -> ad  (page-specific links)
    offer_nopage_to_ad = {} # offer_id -> ad           (links with no page set)
    ad_real_margin = {a.id: Decimal("0") for a in ads}  # net revenue attributed
    ad_own_offers = {a.id: set() for a in ads}          # offer ids the ad is linked to
    ad_extras = {a.id: defaultdict(int) for a in ads}   # extra offer name -> qty
    for a in ads:
        if a.attribution == Ad.ATTR_BARATS:
            continue
        for lk in a.links.all():
            if lk.sales_page_id:
                offer_page_to_ad.setdefault((lk.offer_id, lk.sales_page_id), a)
            else:
                offer_nopage_to_ad.setdefault(lk.offer_id, a)
            ad_own_offers[a.id].add(lk.offer_id)

    # Qualifying orders created in the range, with their offer lines + page.
    # Counted statuses: en_cours / au_magasin (may still be delivered) / livree /
    # payee, PLUS orders scanned out today (ShippingOrder CLOSED) so the current
    # day isn't empty before Navex advances them. We EXCLUDE returns and
    # cancellations (returning / returned / annulee) and EXCHANGE orders
    # (exchange_of set) — those aren't real ad-driven sales.
    from django.db.models import Q as _Q
    RETURN_LIKE = [Order.RETURNING, Order.RETURNED, Order.ANNULEE]
    q_orders = (Order.objects
                .filter(created_at__date__gte=start, created_at__date__lte=end)
                .filter(
                    _Q(status__in=QUALIFYING)
                    | _Q(shipping_orders__status__in=ShippingOrder.CLOSED_STATUSES)
                )
                .exclude(status__in=RETURN_LIKE)
                .filter(exchange_of__isnull=True)
                .distinct()
                .select_related("sales_page")
                .prefetch_related("order_offers"))

    # Index by (offer_id, page_id) and by offer_id (any page). For each we track
    # the total QUANTITY of that offer sold (3× ICY MAZE counts as 3) and the
    # revenue that offer generated (bundle_price × quantity), so an ad's cost is
    # divided by units sold, and revenue is the offer's own share (no
    # over-counting on multi-offer orders).
    from collections import defaultdict
    pair_qty = defaultdict(int)          # (offer_id, page_id) -> qty
    pair_rev = defaultdict(lambda: Decimal("0"))
    offer_qty_any = defaultdict(int)     # offer_id -> qty (any page)
    offer_rev_any = defaultdict(lambda: Decimal("0"))
    page_qty = defaultdict(int)          # page_id -> total qty (ALL offers on page)
    page_rev = defaultdict(lambda: Decimal("0"))
    barats_qty = 0
    barats_rev = Decimal("0")
    for o in q_orders:
        page_id = o.sales_page_id
        page_name = (o.sales_page.name if o.sales_page else "").strip().lower()
        is_barats = page_name == "barats.tn"
        # ---- Real-margin attribution: the whole order's NET revenue
        # (total − delivery_fee, discount already baked into total) goes to the
        # FIRST ad whose linked offer appears in this order. Everything the
        # customer bought (incl. unrelated items) counts, since that campaign
        # brought them. An order is attributed to at most one ad.
        order_net = (o.total or Decimal("0")) - (o.delivery_fee or Decimal("0"))
        attributed_ad = None
        for oo in o.order_offers.all():
            if not oo.offer_id:
                continue
            # Match the ad on (offer, THIS page) first; only if no page-specific
            # link exists do we fall back to a page-less link for that offer.
            # This prevents attributing a Next-Generation ad's margin to orders
            # from a different page that happen to contain the same offer.
            a = (offer_page_to_ad.get((oo.offer_id, page_id))
                 or offer_nopage_to_ad.get(oo.offer_id))
            if a is not None:
                attributed_ad = a
                break
        if attributed_ad is not None:
            ad_real_margin[attributed_ad.id] += order_net
            # Record EXTRA offers in this order — the ones that are NOT among the
            # ad's own linked offers — so the ad card can show "+ Ensemble Pierce
            # ×3" etc. Uses offer name + summed quantity.
            own = ad_own_offers.get(attributed_ad.id, set())
            for oo in o.order_offers.all():
                if oo.offer_id and oo.offer_id not in own:
                    label = oo.offer_name or (oo.offer.name if oo.offer else "?")
                    ad_extras[attributed_ad.id][label] += (oo.quantity or 1)
        for oo in o.order_offers.all():
            if not oo.offer_id:
                continue
            qty = oo.quantity or 1
            rev = (oo.bundle_price or Decimal("0")) * qty
            pair_qty[(oo.offer_id, page_id)] += qty
            pair_rev[(oo.offer_id, page_id)] += rev
            offer_qty_any[oo.offer_id] += qty
            offer_rev_any[oo.offer_id] += rev
            page_qty[page_id] += qty
            page_rev[page_id] += rev
            if is_barats:
                barats_qty += qty
                barats_rev += rev

    total_spend = Decimal("0")

    # --- Section 1: Barats.tn carousel pool (blended, by quantity) ---
    barats_ads = [a for a in ads if a.attribution == Ad.ATTR_BARATS]
    barats_spend = sum((a.spend or Decimal("0")) for a in barats_ads)
    barats_cpo = (barats_spend / barats_qty) if barats_qty else None
    total_spend += barats_spend
    barats_block = {
        "ads": sorted(barats_ads, key=lambda a: a.spend or 0, reverse=True),
        "spend": barats_spend,
        "order_count": barats_qty,
        "revenue": barats_rev,
        "cpo": barats_cpo,
        "profit": barats_rev - barats_spend,
    }

    # --- Section 2: offer-linked ads (Converty / Facebook), by quantity ---
    rows = []
    for ad in ads:
        if ad.attribution == Ad.ATTR_BARATS:
            continue
        spend = ad.spend or Decimal("0")
        total_spend += spend
        links = list(ad.links.all())
        qty_sum = 0
        revenue = Decimal("0")
        link_desc = []
        for lk in links:
            if lk.sales_page_id:
                q = pair_qty.get((lk.offer_id, lk.sales_page_id), 0)
                r = pair_rev.get((lk.offer_id, lk.sales_page_id), Decimal("0"))
            else:
                q = offer_qty_any.get(lk.offer_id, 0)
                r = offer_rev_any.get(lk.offer_id, Decimal("0"))
            qty_sum += q
            revenue += r
            link_desc.append({
                "offer": lk.offer, "offer_id": lk.offer_id,
                "page": lk.sales_page, "page_id": lk.sales_page_id, "qty": q,
            })
        cpo = (spend / qty_sum) if qty_sum else None
        # Real margin = net revenue of orders attributed to this ad − ad spend.
        real_net = ad_real_margin.get(ad.id, Decimal("0"))
        real_margin = real_net - spend
        spend_orig = ad.spend_original or Decimal("0")
        cpo_orig = (spend_orig / qty_sum) if qty_sum else None
        extras = sorted(
            ({"name": nm, "qty": q} for nm, q in ad_extras.get(ad.id, {}).items()),
            key=lambda e: e["qty"], reverse=True,
        )
        rows.append({
            "ad": ad,
            "spend": spend,
            "spend_orig": spend_orig,
            "currency": ad.currency or "TND",
            "cpo_orig": cpo_orig,
            "links": link_desc,
            "order_count": qty_sum,
            "revenue": revenue,
            "cpo": cpo,
            "profit": revenue - spend,
            "real_net": real_net,
            "real_margin": real_margin,
            "extras": extras,
            "status": ad.effective_status or "",
            "page_ids": {lk["page_id"] for lk in link_desc if lk["page_id"]},
        })
    rows.sort(key=lambda r: r["spend"], reverse=True)

    # --- Group ad rows by page, with a per-page summary box (like Barats). ---
    # A page's box shows: total spend of that page's ads, and the TOTAL units /
    # revenue of ALL orders on that page (page_qty / page_rev), plus the ads
    # detailed underneath. An ad with links on several pages appears under each.
    # Ads with no page link at all go into an "unassigned" group.
    page_by_id = {p.id: p for p in pages}
    page_groups = {}   # page_id -> {"page", "spend", "ads":[rows], ...}
    unassigned = []
    for r in rows:
        pids = r["page_ids"]
        if not pids:
            unassigned.append(r)
            continue
        for pid in pids:
            g = page_groups.get(pid)
            if g is None:
                pg = page_by_id.get(pid)
                # Skip Barats.tn here — it has its own carousel pool box.
                if pg and pg.name.strip().lower() == "barats.tn":
                    continue
                g = page_groups[pid] = {
                    "page": pg, "page_id": pid,
                    "spend": Decimal("0"), "ads": [],
                }
            g["spend"] += r["spend"]
            g["ads"].append(r)

    page_blocks = []
    for pid, g in page_groups.items():
        qty = page_qty.get(pid, 0)
        rev = page_rev.get(pid, Decimal("0"))
        spend = g["spend"]
        page_blocks.append({
            "page": g["page"],
            "spend": spend,
            "order_count": qty,            # total units on the page
            "revenue": rev,                # total revenue on the page
            "cpo": (spend / qty) if qty else None,
            "profit": rev - spend,
            "ads": sorted(g["ads"], key=lambda x: x["spend"], reverse=True),
        })
    page_blocks.sort(key=lambda b: b["spend"], reverse=True)

    total_revenue = barats_rev + sum((r["revenue"] for r in rows), Decimal("0"))

    # ---- Advice: which ads to cut vs boost, based on REAL margin.
    # Losing / too thin: real margin < 15 DT AND the ad actually spent something.
    # Boost: the top 3 ads by real net revenue that are also profitable.
    advice_losing = sorted(
        [r for r in rows if r["spend"] > 0 and r["real_margin"] < 15],
        key=lambda r: r["real_margin"],
    )
    advice_boost = sorted(
        [r for r in rows if r["real_margin"] >= 15],
        key=lambda r: r["real_net"], reverse=True,
    )[:3]

    return render(request, "inventory/ads_offers.html", {
        "rows": rows,                 # kept for back-compat / any other use
        "page_blocks": page_blocks,   # per-page summary + detailed ads
        "unassigned": unassigned,     # ads with no page link
        "barats": barats_block,
        "advice_losing": advice_losing,
        "advice_boost": advice_boost,
        "offers": offers,
        "pages": pages,
        "start": start_str,
        "end": end_str,
        "is_today": start == today and end == today,
        "single_day": start == end,
        "total_spend": total_spend,
        "total_revenue": total_revenue,
        "total_profit": total_revenue - total_spend,
        "unlinked_count": sum(1 for a in ads if a.attribution == Ad.ATTR_OFFER and not a.links.exists()),
    })


@csrf_exempt
@require_POST
@login_required(login_url="/login/")
def api_ad_archive(request, pk):
    """Archive or unarchive an ad. Body: {"archived": true|false}.
    Archived ads are hidden from the dashboard and excluded from attribution."""
    if not _orders_role_check(request):
        return JsonResponse({"status": "error", "message": "Accès refusé."}, status=403)
    from .models import Ad
    try:
        ad = Ad.objects.get(pk=pk)
    except Ad.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Publicité introuvable."}, status=404)
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        data = {}
    ad.archived = bool(data.get("archived", True))
    ad.save(update_fields=["archived", "updated_at"])
    return JsonResponse({"status": "ok", "ad_id": ad.id, "archived": ad.archived})


@csrf_exempt
@require_POST
@login_required(login_url="/login/")
def api_ad_link_offer(request, pk):
    """Configure an Ad's attribution.

    Body (JSON):
      {"attribution": "barats"}
        -> Barats.tn carousel pool (clears all links)
      {"attribution": "offer", "pairs": [{"offer_id":1,"page_id":3},
                                         {"offer_id":2,"page_id":3}]}
        -> link 1 or 2 (offer, page) pairs
      {"pairs": []}  -> unlink all (attribution stays 'offer')

    page_id may be null/omitted to mean "any page" for that offer.
    """
    if not _orders_role_check(request):
        return JsonResponse({"status": "error", "message": "Accès refusé."}, status=403)
    from .models import Ad, Offer, SalesPage, AdOfferLink
    try:
        ad = Ad.objects.get(pk=pk)
    except Ad.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Publicité introuvable."}, status=404)
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "JSON invalide."}, status=400)

    attribution = data.get("attribution")

    if attribution == Ad.ATTR_BARATS:
        ad.attribution = Ad.ATTR_BARATS
        ad.offer = None
        ad.save(update_fields=["attribution", "offer", "updated_at"])
        ad.links.all().delete()
        return JsonResponse({"status": "ok", "ad_id": ad.id, "attribution": ad.attribution, "pairs": []})

    # attribution == 'offer' (default)
    ad.attribution = Ad.ATTR_OFFER
    pairs = data.get("pairs")
    if pairs is None:
        # Nothing to change about links; just persist attribution.
        ad.save(update_fields=["attribution", "updated_at"])
        return JsonResponse({"status": "ok", "ad_id": ad.id, "attribution": ad.attribution})

    cleaned = []
    seen = set()
    for p in pairs:
        oid = p.get("offer_id")
        pgid = p.get("page_id")
        if oid in (None, "", "none"):
            continue
        try:
            oid = int(oid)
        except (ValueError, TypeError):
            return JsonResponse({"status": "error", "message": "Offre invalide."}, status=400)
        if not Offer.objects.filter(pk=oid).exists():
            return JsonResponse({"status": "error", "message": "Offre introuvable."}, status=400)
        if pgid in (None, "", "none"):
            pgid = None
        else:
            try:
                pgid = int(pgid)
            except (ValueError, TypeError):
                return JsonResponse({"status": "error", "message": "Page invalide."}, status=400)
            if not SalesPage.objects.filter(pk=pgid).exists():
                return JsonResponse({"status": "error", "message": "Page introuvable."}, status=400)
        key = (oid, pgid)
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(key)

    # Replace all links atomically.
    ad.links.all().delete()
    for oid, pgid in cleaned:
        AdOfferLink.objects.create(ad=ad, offer_id=oid, sales_page_id=pgid)
    ad.offer_id = cleaned[0][0] if cleaned else None  # keep legacy FK in sync
    ad.save(update_fields=["attribution", "offer", "updated_at"])

    return JsonResponse({
        "status": "ok", "ad_id": ad.id, "attribution": ad.attribution,
        "pairs": [{"offer_id": o, "page_id": p} for o, p in cleaned],
    })


@login_required(login_url="/login/")
def diagnose_offer_web(request):
    """TEMPORARY admin-only diagnostic. Open /diagnose-offer/?name=Icy%20Maze
    (or ?offer_id=NN) in a browser. Prints offer -> product -> family ->
    variants -> unit counts by size as plain text. Remove after debugging."""
    if not _orders_role_check(request):
        return HttpResponse("Accès refusé.", status=403, content_type="text/plain; charset=utf-8")
    from .models import Offer, Product, ProductUnit
    from django.db.models import Q

    name = (request.GET.get("name") or "").strip()
    offer_id = (request.GET.get("offer_id") or "").strip()
    qs = Offer.objects.all()
    if offer_id.isdigit():
        qs = qs.filter(id=int(offer_id))
    elif name:
        qs = qs.filter(name__icontains=name)
    else:
        return HttpResponse("Ajoutez ?name=Icy Maze ou ?offer_id=NN à l'URL.",
                            content_type="text/plain; charset=utf-8")

    lines = []
    for offer in qs:
        lines.append(f"=== OFFER #{offer.id}: {offer.name} (active={offer.is_active}) ===")
        ops = offer.products.all()
        if not ops:
            lines.append("  (offer has NO products linked)")
        for op in ops:
            p = op.product
            root = p.parent_product or p
            family = Product.objects.filter(Q(id=root.id) | Q(parent_product=root))
            lines.append(f"  OfferProduct -> product #{p.id} '{p.name}' "
                         f"(parent={p.parent_product_id}) qty={op.quantity}")
            lines.append(f"    root=#{root.id} '{root.name}'  family size={family.count()}")
            for fam_p in family:
                variants = fam_p.variants.all()
                lines.append(f"      product #{fam_p.id} '{fam_p.name}' -> {variants.count()} variant(s)")
                for v in variants:
                    by_size = {}
                    for u in v.units.all():
                        by_size[u.size] = by_size.get(u.size, 0) + 1
                    lines.append(f"        variant #{v.id} color='{v.color_label or v.color_name}' "
                                 f"units={v.units.count()} by_size={by_size}")
        lines.append("")
    if not lines:
        lines = [f"No offer matched name='{name}' offer_id='{offer_id}'."]
    return HttpResponse("\n".join(lines), content_type="text/plain; charset=utf-8")


# ---- DM order intake (called by n8n / Messenger pipeline) -------------------

@csrf_exempt
@require_POST
def api_n8n_create_order_from_dm(request):
    """Create a PENDING order from a Messenger DM, called by n8n.

    This is machine-to-machine (no user login). It is protected by a shared
    secret header instead: n8n must send  X-DM-Token: <DM_INTAKE_TOKEN>
    matching the env var, otherwise the request is rejected.

    Expected JSON:
    {
      "phone": "29876313",            # required (customer key)
      "name": "Alaeddine",            # optional
      "psid": "PSID_123",             # Messenger Page-Scoped ID (linking key)
      "conversation": "full chat...", # the conversation text to attach
      "source_ad": "120244285501770755",  # optional ad id for attribution
      "sales_page": <id>              # optional; falls back to default if omitted
    }

    Deliberate design:
    - Creates a NON_CONFIRMEE (pending) order only. NEVER auto-confirmed — a
      human reviews and completes it. This is the spec's golden rule for COD.
    - Stores conversation_text + conversation_updated_at on the order, and
      customer_psid on the customer (permanent linking key).
    - Does NOT try to build order lines here. It captures the customer +
      conversation + ad source as a pending draft; the team completes the
      articles. (AI line-item extraction is a later, separate layer.)
    """
    import os
    from django.utils import timezone
    from .models import Order, Customer, SalesPage, log_action, AuditLog

    # --- auth: shared secret header ---
    expected = (os.environ.get("DM_INTAKE_TOKEN") or "").strip()
    provided = (request.headers.get("X-DM-Token") or "").strip()
    if not expected or provided != expected:
        return JsonResponse({"status": "error", "message": "Unauthorized."}, status=401)

    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "JSON invalide."}, status=400)

    phone = (data.get("phone") or "").strip()
    if not phone:
        return JsonResponse({"status": "error", "message": "Téléphone obligatoire."}, status=400)

    name = (data.get("name") or "").strip()
    psid = (data.get("psid") or "").strip()
    conversation = (data.get("conversation") or "").strip()
    source_ad = (data.get("source_ad") or "").strip()

    try:
        # Customer: same phone = same customer. Update name/psid if provided.
        customer, _created = Customer.objects.get_or_create(phone=phone, defaults={"name": name})
        changed = []
        if name and customer.name != name:
            customer.name = name; changed.append("name")
        if psid and customer.customer_psid != psid:
            customer.customer_psid = psid; changed.append("customer_psid")
        if changed:
            customer.save(update_fields=changed)

        # Sales page: use provided, else first available (best-effort default).
        sales_page_id = data.get("sales_page")
        if not sales_page_id:
            sp = SalesPage.objects.first()
            sales_page_id = sp.id if sp else None

        # Note line records the ad source for traceability even before a
        # dedicated attribution field exists.
        note_bits = ["[Commande créée depuis Messenger]"]
        if source_ad:
            note_bits.append(f"Ad source: {source_ad}")

        order = Order.objects.create(
            customer=customer,
            sales_page_id=sales_page_id,
            status=Order.NON_CONFIRMEE,
            notes="\n".join(note_bits),
            conversation_text=conversation,
            conversation_updated_at=timezone.now() if conversation else None,
            created_by=None,
        )

        log_action(
            None, AuditLog.CREATE,
            description=f"Commande #{order.id} créée depuis Messenger pour {customer} (en attente de confirmation)",
            target_model="Order", target_id=order.id,
        )
        return JsonResponse({
            "status": "ok",
            "order_id": order.id,
            "customer_id": customer.id,
            "message": "Commande en attente créée.",
        })
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)}, status=400)


# ---- DM conversation: refresh / fetch latest --------------------------------

@csrf_exempt
@require_POST
@login_required(login_url="/login/")
@csrf_exempt
@require_POST
def api_conversation_send_message(request, pk):
    """Send a manual message from staff to the customer of this order's linked
    Messenger/Instagram conversation. Uses the same send path as the bot and
    appends the message to the conversation so it shows in the chat view."""
    from .models import Order, MessengerConversation
    if not _orders_role_check(request):
        return JsonResponse({"status": "error", "message": "Accès refusé."}, status=403)
    try:
        import json as _json
        data = _json.loads(request.body.decode("utf-8"))
        text = (data.get("text") or "").strip()
        if not text:
            return JsonResponse({"status": "error", "message": "Message vide."}, status=400)

        order = Order.objects.get(pk=pk)
        conv = (MessengerConversation.objects
                .filter(pending_order_id=order.id).order_by("-id").first())
        if not conv:
            return JsonResponse({"status": "error",
                                 "message": "Aucune conversation liée à cette commande."},
                                status=404)

        page_id = str(conv.page_id or "")
        sender_id = str(conv.sender_id or "")
        platform = conv.platform or "messenger"
        if not page_id or not sender_id:
            return JsonResponse({"status": "error",
                                 "message": "Conversation sans identifiants d'envoi."},
                                status=400)

        ok = _messenger_send_text(page_id, sender_id, text, platform)
        if not ok:
            return JsonResponse({"status": "error",
                                 "message": "Meta a refusé l'envoi (fenêtre 24h dépassée ou permission)."},
                                status=502)

        # Append to the conversation so it appears in the chat view.
        try:
            mm = conv.messages or []
            mm.append({"from": "page", "text": text, "ts": "", "mid": "",
                       "manual": True})
            conv.messages = mm
            conv.save(update_fields=["messages", "updated_at"])
        except Exception:
            pass

        return JsonResponse({"status": "ok", "sent": text})
    except Order.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Commande introuvable."}, status=404)
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)[:200]}, status=500)


def api_order_refresh_conversation(request, pk):
    """Return the latest stored Messenger conversation for this order.

    For now this simply returns what is stored on the order. It is the hook
    where a live re-fetch will plug in later: when the Messenger webhook /
    n8n integration is live, this endpoint can call out to pull the current
    conversation for the customer's PSID and update conversation_text.

    Honest constraint (see v2 spec): a live re-fetch only reliably works when
    the customer has sent a recent message (open messaging window). It cannot
    pull an arbitrary silent old conversation on demand.
    """
    from .models import Order
    if not _orders_role_check(request):
        return JsonResponse({"status": "error", "message": "Accès refusé."}, status=403)
    try:
        order = Order.objects.select_related("customer").get(pk=pk)
    except Order.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Commande introuvable."}, status=404)

    psid = (order.customer.customer_psid or "").strip() if order.customer_id else ""

    # If a MessengerConversation is linked to this order, return its structured
    # messages (with image attachments) so the chat view can show photos.
    structured = []
    platform = ""
    ad_source = {}
    try:
        from .models import MessengerConversation
        conv = MessengerConversation.objects.filter(pending_order_id=order.id).order_by("-id").first()
        if conv:
            platform = conv.platform or ""
            if conv.source_ad_id or conv.source_campaign or conv.source_campaign_name:
                ad_source = {
                    "ad_id": conv.source_ad_id or "",
                    "campaign": conv.source_campaign_name or conv.source_campaign or "",
                    "post_title": conv.source_campaign or "",
                    "ref": conv.source_ad_ref or "",
                }
            if conv.messages:
                for m in conv.messages:
                    structured.append({
                        "from": m.get("from", "user"),
                        "text": m.get("text", ""),
                        "images": m.get("images", []),
                    })
    except Exception:
        structured = []

    if order.conversation_text:
        return JsonResponse({
            "status": "ok",
            "conversation_text": order.conversation_text,
            "messages": structured,
            "platform": platform,
            "ad_source": ad_source,
            "updated_at": order.conversation_updated_at.strftime("%d/%m/%Y %H:%M") if order.conversation_updated_at else "",
        })
    if psid:
        return JsonResponse({
            "status": "ok",
            "conversation_text": "",
            "message": "Aucune conversation enregistrée. Elle réapparaîtra si le client envoie un nouveau message.",
        })
    return JsonResponse({
        "status": "ok",
        "conversation_text": "",
        "message": "Cette commande n'est pas liée à une conversation Messenger.",
    })


# ---- Exchange: return items APIs --------------------------------------------

@login_required(login_url="/login/")
def api_exchange_source_items(request, pk):
    """For an EXCHANGE order, returns the list of items that were in the
    original delivered order (so the office worker can pick which ones the
    client returns).
    """
    from .models import Order
    if not _orders_role_check(request):
        return JsonResponse({"status": "error"}, status=403)
    try:
        exchange = Order.objects.select_related("exchange_of").get(pk=pk)
    except Order.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Commande introuvable."}, status=404)
    if not exchange.exchange_of_id:
        return JsonResponse({"status": "error", "message": "Cette commande n'est pas un échange."}, status=400)

    source = exchange.exchange_of
    items = []
    # Walk through the original order's OrderLines.
    # Each OrderLine has product, variant, size, quantity directly — NO physical unit
    # at this stage (units get attached later during scan expedition).
    # Group by (variant, size) so we get one card per "variant + size" combo.
    from collections import OrderedDict
    grouped = OrderedDict()
    for line in source.lines.select_related("product", "variant").all():
        variant_id = line.variant_id
        size = line.size or ""
        # Combine quantity across multiple lines with the same variant+size
        key = (variant_id, size)
        if key not in grouped:
            grouped[key] = {
                "variant_id": variant_id,
                "size": size,
                "product_name": line.product.name if line.product else "",
                "color_label": line.variant.color_label if line.variant else "",
                "image_url": line.variant.image.url if line.variant and line.variant.image else None,
                "qty": 0,
            }
        grouped[key]["qty"] += line.quantity or 1

    items = list(grouped.values())

    # Attach the physical unit barcodes from the original order, matched by
    # variant + size. Units are reachable via the v2 Order's v1 ShippingOrders
    # → OrderItems → unit. This lets the office worker see which barcode(s)
    # correspond to each item being returned.
    barcodes_by_key = {}
    barcodes_by_variant = {}   # variant_id -> units
    barcodes_by_color = {}     # color_label(lower) -> units
    for so in source.shipping_orders.all():
        for oi in so.items.select_related("unit__variant").all():
            unit = oi.unit
            if not unit:
                continue
            entry = {
                "barcode": unit.barcode,
                "status": unit.status,
                "status_label": unit.get_status_display(),
                "size": unit.size or "",
            }
            k = (unit.variant_id, unit.size or "")
            barcodes_by_key.setdefault(k, []).append(entry)
            barcodes_by_variant.setdefault(unit.variant_id, []).append(entry)
            clbl = ((unit.variant.color_label if unit.variant else "")
                    or (unit.variant.color_name if unit.variant else "")).strip().lower()
            if clbl:
                barcodes_by_color.setdefault(clbl, []).append(entry)
    # Map each item's variant to its colour label for the colour fallback.
    from .models import ProductVariant
    for it in items:
        # 1) exact variant+size
        units = barcodes_by_key.get((it["variant_id"], it["size"]), [])
        # 2) same variant, any size
        if not units:
            units = barcodes_by_variant.get(it["variant_id"], [])
        # 3) same COLOUR across product versions (variant IDs differ between
        #    V1/V2 of the same item, but the colour matches — e.g. line points
        #    to variant 44 BLUE while the scanned unit is variant 109 BLUE V2).
        if not units:
            clbl = (it.get("color_label") or "").strip().lower()
            if clbl:
                units = barcodes_by_color.get(clbl, [])
        it["units"] = units

    # Also check what's already been selected (if return_items already exist for this exchange)
    selected = list(exchange.return_items.values_list("variant_id", "size"))
    selected_set = set((vid, sz) for vid, sz in selected)
    for it in items:
        key = (it["variant_id"], it["size"])
        it["is_selected"] = key in selected_set

    return JsonResponse({
        "status": "ok",
        "source_order_id": source.id,
        "items": items,
    })


@csrf_exempt
@require_POST
@login_required(login_url="/login/")
def api_exchange_set_returns(request, pk):
    """Save the list of items the customer is returning for this exchange.
    Body: {items: [{variant_id, size, qty}, ...]}
    """
    from .models import Order, ExchangeReturnItem, ProductVariant
    if not _orders_role_check(request):
        return JsonResponse({"status": "error"}, status=403)
    try:
        exchange = Order.objects.select_related("exchange_of").get(pk=pk)
    except Order.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Commande introuvable."}, status=404)
    if not exchange.exchange_of_id:
        return JsonResponse({"status": "error", "message": "Cette commande n'est pas un échange."}, status=400)
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "JSON invalide."}, status=400)

    raw_items = data.get("items") or []
    # Wipe existing return_items and re-create from the new selection
    exchange.return_items.all().delete()

    created = 0
    claimed = set()  # original-order unit ids already linked, across all selections
    original = exchange.exchange_of
    from .models import ProductUnit

    # ---- Build a pool of the REAL physical units from the original delivered
    # order, so every return item gets linked to an actual barcode (e.g.
    # PRC2-BLU-5-016) instead of a RETURN-<id> placeholder. Units are reachable
    # via the v2 Order → v1 ShippingOrders → OrderItems → unit.
    # We index the same three ways the display endpoint does, so matching here
    # is guaranteed to line up with what the office worker sees.
    units_by_key = {}      # (variant_id, size_str) -> [units]
    units_by_variant = {}  # variant_id -> [units]
    units_by_color = {}    # color_label_lower -> [units]
    all_units = []
    if original is not None:
        for so in original.shipping_orders.all():
            for oi in so.items.select_related("unit__variant").all():
                u = oi.unit
                if not u:
                    continue
                all_units.append(u)
                units_by_key.setdefault((u.variant_id, str(u.size or "")), []).append(u)
                units_by_variant.setdefault(u.variant_id, []).append(u)
                clbl = ((u.variant.color_label if u.variant else "")
                        or (u.variant.color_name if u.variant else "")).strip().lower()
                if clbl:
                    units_by_color.setdefault(clbl, []).append(u)

    def _take_unit(variant, size):
        """Pick one still-unclaimed real unit for this variant+size, using the
        same layered fallback as the display: exact variant+size → same variant
        → same colour. Returns a ProductUnit or None."""
        size_str = str(size or "")
        pools = [
            units_by_key.get((variant.id, size_str), []),
            units_by_variant.get(variant.id, []),
        ]
        clbl = ((variant.color_label or variant.color_name or "").strip().lower())
        if clbl:
            pools.append(units_by_color.get(clbl, []))
        for pool in pools:
            for u in pool:
                if u.id not in claimed:
                    claimed.add(u.id)
                    return u
        return None

    for it in raw_items:
        try:
            variant_id = int(it.get("variant_id"))
            size = (it.get("size") or "").strip()
            qty = int(it.get("qty") or 1)
        except (TypeError, ValueError):
            continue
        try:
            variant = ProductVariant.objects.select_related("product").get(pk=variant_id)
        except ProductVariant.DoesNotExist:
            continue

        # Preferred path: the frontend sends the exact unit barcodes selected.
        # Link those precise units so the return scan shows the right barcode.
        sent_barcodes = it.get("barcodes") or []
        used_from_sent = 0
        for bc in sent_barcodes:
            if used_from_sent >= qty:
                break
            u = ProductUnit.objects.filter(barcode=bc).first()
            if u and u.id not in claimed:
                ExchangeReturnItem.objects.create(
                    exchange_order=exchange,
                    unit=u,
                    variant=variant,
                    size=size or (str(u.size) if u.size else ""),
                    product_name_snapshot=variant.product.name,
                )
                claimed.add(u.id)
                used_from_sent += 1
                created += 1

        # For any remaining qty with no explicit barcode, always try to attach a
        # real unit from the original delivered order. Only if the original has
        # no matchable unit at all do we fall back to unit=None.
        remaining = qty - used_from_sent
        for _ in range(remaining):
            matched_unit = _take_unit(variant, size)
            ExchangeReturnItem.objects.create(
                exchange_order=exchange,
                unit=matched_unit,
                variant=variant,
                size=size or (str(matched_unit.size) if matched_unit and matched_unit.size else ""),
                product_name_snapshot=variant.product.name,
            )
            created += 1

    return JsonResponse({
        "status": "ok",
        "created": created,
        "total": exchange.return_items.count(),
    })


# ---- Shopify webhook receiver -----------------------------------------------

@csrf_exempt
@require_POST
def api_shopify_webhook_order_created(request):
    """Receive a Shopify 'order/create' webhook and create a v2 Order draft.

    Workflow:
      1. Verify HMAC signature (env var SHOPIFY_WEBHOOK_SECRET)
      2. Parse Shopify's JSON order payload
      3. Match each line item to a Product by fuzzy name (case-insensitive)
      4. Create Customer + Order (status=non_confirmee) + OrderLines
      5. Log to AuditLog

    Auth: NONE — Shopify can't login. Trust comes from the HMAC signature.
    """
    import hmac, hashlib, base64
    from .models import (
        Customer, Order, OrderLine, Product, ProductVariant,
        SalesPage, Region, AuditLog, log_action,
    )

    # 1. Verify HMAC signature
    secret = os.environ.get("SHOPIFY_WEBHOOK_SECRET", "")
    received_hmac = request.headers.get("X-Shopify-Hmac-Sha256", "")
    if not secret:
        # If the env var isn't set, refuse the request — never accept unsigned data
        return JsonResponse({"status": "error", "message": "Webhook secret non configuré."}, status=503)
    body = request.body
    computed = base64.b64encode(
        hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    ).decode("utf-8")
    if not hmac.compare_digest(computed, received_hmac):
        # Bad signature — could be an attacker
        log_action(
            None, AuditLog.OTHER,
            description=f"Webhook Shopify REJETÉ : signature HMAC invalide (IP {request.META.get('REMOTE_ADDR', '?')})",
        )
        return JsonResponse({"status": "error", "message": "Signature invalide."}, status=401)

    # 2. Parse payload
    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"status": "error", "message": "Payload JSON invalide."}, status=400)

    shopify_order_id = str(payload.get("id") or "")
    return _create_order_from_shopify_shaped_payload(
        payload,
        source="shopify",
        external_id=shopify_order_id,
        request=request,
    )


def _maybe_send_status_sms(order):
    """Send the customer SMS appropriate to the order's CURRENT status, once.
    Covers: injoignable, expédié (en_cours covers shipped+delivery in our flow),
    en_cours (with livreur phone + amount). Best-effort, never raises."""
    try:
        from . import sms_service
    except Exception:
        return
    if not order or not order.customer:
        return
    phone = order.customer.phone
    try:
        if order.status == Order.INJOIGNABLE and not order.sms_injoignable_sent:
            ok, _ = sms_service.send_sms(phone, sms_service.msg_injoignable())
            if ok:
                order.sms_injoignable_sent = True
                order.save(update_fields=["sms_injoignable_sent"])
        elif order.status == Order.EN_COURS and not order.sms_en_cours_sent:
            total = sms_service._fmt_total(order)
            tel = (order.navex_livreur_tel or "").strip()
            ok, _ = sms_service.send_sms(phone, sms_service.msg_en_cours(total, tel))
            if ok:
                order.sms_en_cours_sent = True
                order.save(update_fields=["sms_en_cours_sent"])
    except Exception:
        pass


def _maybe_send_expedie_sms(order):
    """Send the 'expédié' SMS once when an order's colis is shipped."""
    try:
        from . import sms_service
    except Exception:
        return
    if not order or not order.customer or order.sms_expedie_sent:
        return
    try:
        total = sms_service._fmt_total(order)
        ok, _ = sms_service.send_sms(order.customer.phone, sms_service.msg_expedie(total))
        if ok:
            order.sms_expedie_sent = True
            order.save(update_fields=["sms_expedie_sent"])
    except Exception:
        pass


def _create_order_from_shopify_shaped_payload(payload, source="shopify", external_id="", request=None, sales_page_id=None):
    """Shared order-creation engine. Takes a Shopify-shaped payload (the Converty
    webhook builds one too) and runs the full matching/AI/region pipeline,
    creating a v2 Order. `source` is 'shopify' or 'converty'; `external_id` is
    the originating order id used for dedup and (for Converty) status push-back.
    """
    from .models import (
        Customer, Order, OrderLine, Product, ProductVariant,
        SalesPage, Region, AuditLog, log_action,
    )
    import hmac, hashlib, base64

    shopify_order_id = str(external_id or "")
    shopify_order_number = str(payload.get("order_number") or payload.get("name") or "")

    # 3. Extract customer info
    # Shopify's customer object is in `customer` (registered) or `billing_address` / `shipping_address`
    shipping = payload.get("shipping_address") or {}
    billing = payload.get("billing_address") or {}
    customer_data = payload.get("customer") or {}

    phone_raw = (
        shipping.get("phone")
        or billing.get("phone")
        or customer_data.get("phone")
        or payload.get("phone")
        or ""
    )
    # Normalize Tunisian phone: strip everything except digits, take last 8 digits
    phone_digits = "".join(c for c in str(phone_raw) if c.isdigit())
    if len(phone_digits) >= 8:
        phone_norm = phone_digits[-8:]
    else:
        phone_norm = phone_digits
    if not phone_norm:
        # Without a phone we can't deliver — log and bail
        log_action(
            None, AuditLog.OTHER,
            description=f"Webhook Shopify reçu pour #{shopify_order_number} mais SANS téléphone — ignoré.",
        )
        return JsonResponse({"status": "ok", "message": "No phone, ignored."})

    def _safe_name(d):
        """Build 'first last' from a Shopify address dict, filtering out
        None / 'None' / empty strings (which the customer sometimes leaves)."""
        if not d:
            return ""
        first = (d.get("first_name") or "").strip()
        last = (d.get("last_name") or "").strip()
        # Some forms send the literal string "None" or "null"
        if first.lower() in ("none", "null"):
            first = ""
        if last.lower() in ("none", "null"):
            last = ""
        parts = [p for p in (first, last) if p]
        return " ".join(parts)

    name = (
        _safe_name(shipping)
        or _safe_name(billing)
        or _safe_name(customer_data)
    )
    address1_raw = shipping.get("address1") or billing.get("address1") or ""
    address2_raw = shipping.get("address2") or billing.get("address2") or ""
    city_raw = (shipping.get("city") or billing.get("city") or "").strip()
    province_raw = (shipping.get("province") or billing.get("province") or "").strip()

    # ---- Arabic → French translation (covers ALL fields, not just province) ----
    # The customer may type the region/city in any field, in Arabic or French.
    # We translate everything first, then search for a matching Region/Delegation
    # across ALL fields (province, city, address1, address2).
    import unicodedata, re

    arabic_to_french = {
        # === 24 Governorates ===
        "تونس": "Tunis", "أريانة": "Ariana", "اريانة": "Ariana",
        "بن عروس": "Ben Arous", "بنعروس": "Ben Arous",
        "منوبة": "Manouba", "منوّبة": "Manouba",
        "نابل": "Nabeul", "زغوان": "Zaghouan",
        "بنزرت": "Bizerte", "باجة": "Beja", "جندوبة": "Jendouba",
        "الكاف": "Kef", "سليانة": "Siliana",
        "القيروان": "Kairouan", "القصرين": "Kasserine",
        "سيدي بوزيد": "Sidi Bouzid", "سيديبوزيد": "Sidi Bouzid",
        "سوسة": "Sousse", "المنستير": "Monastir", "المهدية": "Mahdia",
        "صفاقس": "Sfax", "صفاڤس": "Sfax",
        "قفصة": "Gafsa", "ڤفصة": "Gafsa",
        "توزر": "Tozeur", "قبلي": "Kebili", "ڤبلي": "Kebili",
        "قابس": "Gabes", "ڤابس": "Gabes",
        "مدنين": "Medenine", "تطاوين": "Tataouine",
        # === Delegations / common cities ===
        "الحمامات": "Hammamet", "نابول": "Nabeul",
        "جربة": "Houmt Souk", "حومة السوق": "Houmt Souk",
        "الرڤاب": "Regueb", "الرقاب": "Regueb",
        "أنفيدها": "Enfidha", "النفيضة": "Enfidha",
        "نفطة": "Nefta", "النفطة": "Nefta",
        "دوز": "Douz", "زرزيس": "Zarzis", "جرجيس": "Zarzis",
        "بن قردان": "Ben Gardane", "بنقردان": "Ben Gardane",
        "المرسى": "La Marsa", "قرطاج": "Carthage",
        "سيدي بوسعيد": "Sidi Bou Said", "قمرت": "Gammarth",
        "حلق الوادي": "La Goulette", "رادس": "Rades",
        "حمام الأنف": "Hammam Lif", "حمام الشط": "Hammam Chatt",
        "حمام سوسة": "Hammam Sousse", "أكودة": "Akouda",
        "هرڤلة": "Hergla", "بوفيشة": "Bouficha",
        "سيدي بوعلي": "Sidi Bou Ali", "المساكن": "Msaken",
        "القنطاوي": "Port El Kantaoui",
        "متلوي": "Metlaoui", "أم العرائس": "Oum Larayes",
        "الرديف": "Redeyef",
        "بوسالم": "Bou Salem", "طبرقة": "Tabarka",
        "عين دراهم": "Ain Draham",
        "تستور": "Testour", "ماطر": "Mateur",
        "منزل بورقيبة": "Menzel Bourguiba", "منزل جميل": "Menzel Jemil",
        "غار الملح": "Ghar El Melh",
        "رأس الجبل": "Ras Jebel",
        "قليبية": "Kelibia", "منزل تميم": "Menzel Temime",
        "قربة": "Korba", "بني خلاد": "Beni Khalled",
        "سليمان": "Soliman", "قرمبالية": "Grombalia",
        "دار شعبان الفهري": "Dar Chaabane",
        "بنبلة": "Bembla", "جمال": "Jemmal",
        "قصور الساف": "Ksour Essef", "الجم": "El Jem",
        "بومرداس": "Boumerdes",
        "مكنين": "Moknine", "تبلبة": "Teboulba",
        "جبنيانة": "Jebeniana",
        "الحامة": "El Hamma", "مارث": "Mareth",
        "متماطة": "Matmata",
        "المظيلة": "Mdhilla", "السند": "Sened",
        "الڤطار": "El Guettar",
        "الذكارة": "Dkhara",
        "سيدي صالح": "Sidi Saleh",
    }

    # Arabic letter → Latin transliteration (Tunisian-style).
    # Used as a FALLBACK after the named-place dictionary, so unknown words
    # (street names, neighborhoods, family names) are written in latin script
    # instead of being left in Arabic.
    arabic_translit = {
        # Hamzas / alif variants
        "ء": "'", "آ": "a", "أ": "a", "ؤ": "u", "إ": "i", "ئ": "i", "ا": "a",
        # Letters
        "ب": "b", "ة": "a", "ت": "t", "ث": "th",
        "ج": "j", "ح": "h", "خ": "kh",
        "د": "d", "ذ": "dh",
        "ر": "r", "ز": "z",
        "س": "s", "ش": "ch",
        "ص": "s", "ض": "dh",
        "ط": "t", "ظ": "dh",
        "ع": "a", "غ": "gh",
        "ف": "f",
        "ق": "k", "ڨ": "g", "ڤ": "g",  # Tunisian Q→G variants
        "ك": "k",
        "ل": "l", "م": "m", "ن": "n",
        "ه": "h",
        "و": "w", "ى": "a", "ي": "y",
        # Diacritics — strip
        "َ": "", "ُ": "", "ِ": "", "ّ": "", "ْ": "", "ً": "", "ٌ": "", "ٍ": "",
        # Arabic-Indic digits
        "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
        "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
        # Common punctuation
        "،": ",", "؛": ";", "؟": "?", "ـ": "",
    }

    def _transliterate_arabic_chars(text):
        """Convert any remaining Arabic letters to their latin equivalents
        (mechanical fallback when Gemini is unavailable)."""
        if not text:
            return ""
        out = []
        for ch in text:
            if "\u0600" <= ch <= "\u06ff":
                out.append(arabic_translit.get(ch, ""))
            else:
                out.append(ch)
        return "".join(out)

    def _gemini_transliterate(text):
        """Transliterate Arabic → Latin (Tunisian style). Now routes to Claude
        (Anthropic); falls back to Gemini if no Anthropic key. Returns the
        transliterated text, or None on failure (caller falls back)."""
        if not text:
            return None
        prompt = (
            "Translitère ce texte arabe (tunisien) en lettres latines lisibles "
            "(style phonétique français). N'ajoute aucun commentaire, aucune ponctuation "
            "supplémentaire, aucune explication. Réponds UNIQUEMENT avec le texte translittéré. "
            "Si une partie est déjà en latin, garde-la telle quelle.\n\n"
            f"Texte : {text}"
        )
        if os.environ.get("ANTHROPIC_API_KEY", "").strip():
            return _claude_generate(prompt, max_tokens=256, temperature=0.0)
        return _gemini_transliterate_legacy(text)

    def _gemini_transliterate_legacy(text):
        """Call Google Gemini API to transliterate Arabic → Latin (Tunisian style).
        Returns the transliterated text, or None on failure (caller falls back).
        Supports both classic API keys (AIza...) and new-format ones (AQ...).
        """
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key or not text:
            return None
        import urllib.request as _ureq
        import json as _json
        prompt = (
            "Translitère ce texte arabe (tunisien) en lettres latines lisibles "
            "(style phonétique français). N'ajoute aucun commentaire, aucune ponctuation "
            "supplémentaire, aucune explication. Réponds UNIQUEMENT avec le texte translittéré. "
            "Si une partie est déjà en latin, garde-la telle quelle.\n\n"
            f"Texte : {text}"
        )
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": 256,
            },
        }
        base_url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-2.5-flash-lite:generateContent"
        )
        data = _json.dumps(body).encode("utf-8")

        # Decide auth scheme based on key prefix.
        # - "AIza..." → classic API key, passed via ?key=
        # - "AQ..." or anything else → OAuth-style token, passed via Authorization: Bearer
        attempts = []
        if api_key.startswith("AIza"):
            attempts.append(("query", base_url + "?key=" + api_key, None))
        else:
            # Try Bearer first, then x-goog-api-key header as fallback
            attempts.append(("bearer", base_url, {"Authorization": "Bearer " + api_key}))
            attempts.append(("header", base_url, {"x-goog-api-key": api_key}))
            attempts.append(("query", base_url + "?key=" + api_key, None))

        last_error = None
        for mode, url, headers in attempts:
            # Try up to 2 times with backoff on 429
            for retry_attempt in range(4):
                try:
                    req = _ureq.Request(url, data=data, method="POST")
                    req.add_header("Content-Type", "application/json")
                    if headers:
                        for h, v in headers.items():
                            req.add_header(h, v)
                    with _ureq.urlopen(req, timeout=8) as resp:
                        resp_data = _json.loads(resp.read().decode("utf-8"))
                    candidates = resp_data.get("candidates") or []
                    if not candidates:
                        last_error = "no candidates"
                        break
                    parts = candidates[0].get("content", {}).get("parts") or []
                    if not parts:
                        last_error = "no parts"
                        break
                    result = (parts[0].get("text") or "").strip()
                    if result:
                        return result
                    break
                except Exception as e:
                    last_error = f"{mode}: {type(e).__name__}: {e}"
                    # Retry transient failures (503 overloaded, 429 rate limit,
                    # 500/502, timeouts) with backoff before giving up.
                    es = str(e)
                    transient = ("503" in es or "429" in es or "500" in es
                                 or "502" in es or "timed out" in es.lower())
                    if transient and retry_attempt < 3:
                        import time as _time
                        _time.sleep(2 ** retry_attempt)
                        continue
                    break

        # All attempts failed
        try:
            import logging
            logging.getLogger(__name__).warning("Gemini transliteration failed: %s", last_error)
        except Exception:
            pass
        return None

    def _translate_arabic(text):
        """Replace any Arabic word in `text` with its French equivalent if known.
        Words we don't know are TRANSLITERATED via Gemini (batched per webhook,
        see _gemini_batch_translit) or mechanical fallback.
        """
        if not text:
            return ""
        # If no Arabic chars, return as-is
        if not any("\u0600" <= c <= "\u06ff" for c in text):
            return text
        # Try whole-string match first (for multi-word keys like "سيدي بوزيد")
        stripped = text.strip()
        if stripped in arabic_to_french:
            return arabic_to_french[stripped]
        # Multi-word substring replacement
        result = text
        for k in sorted(arabic_to_french.keys(), key=lambda x: -len(x)):
            if k in result:
                result = result.replace(k, " " + arabic_to_french[k] + " ")
        # Don't call Gemini per-field here. The webhook will batch all remaining
        # Arabic fields in ONE call (see _batch_transliterate below) to stay
        # under per-second quota.
        result = re.sub(r"\s+", " ", result).strip()
        return result

    def _batch_transliterate(fields_dict):
        """Take a dict {name: text} where some texts may contain Arabic.
        Make ONE Gemini call with all Arabic fields, then for each field that
        was still Arabic, replace it with the AI result.
        Fallback to mechanical translit if Gemini fails.

        Returns the updated dict (only Arabic-containing fields are modified).
        """
        # Filter out fields that don't contain Arabic
        arabic_items = [(k, v) for k, v in fields_dict.items()
                        if v and any("\u0600" <= c <= "\u06ff" for c in v)]
        if not arabic_items:
            return fields_dict
        # Build a single prompt with numbered items
        numbered = "\n".join(f"{i+1}. {v}" for i, (_, v) in enumerate(arabic_items))
        prompt = (
            "Translitère les textes arabes (tunisiens) suivants en lettres latines "
            "lisibles (style phonétique français). Garde le numéro et l'ordre. "
            "N'ajoute aucun commentaire, aucune explication. "
            "Si une partie est déjà en latin, garde-la telle quelle.\n\n"
            f"{numbered}"
        )
        ai_response = _gemini_transliterate(prompt)
        if ai_response:
            # Parse the numbered response back
            translated_by_idx = {}
            for line in ai_response.split("\n"):
                line = line.strip()
                if not line:
                    continue
                m = re.match(r"^(\d+)\.\s*(.+)$", line)
                if m:
                    translated_by_idx[int(m.group(1))] = m.group(2).strip()
            # Apply translations if we got the right count
            for i, (k, _v) in enumerate(arabic_items):
                idx = i + 1
                if idx in translated_by_idx:
                    candidate = translated_by_idx[idx]
                    # Only use Gemini result if it actually removed Arabic
                    if not any("\u0600" <= c <= "\u06ff" for c in candidate):
                        fields_dict[k] = candidate
                        continue
                # Fallback mechanical
                fields_dict[k] = _transliterate_arabic_chars(fields_dict[k])
        else:
            # Gemini failed entirely — mechanical for all
            for k, _v in arabic_items:
                fields_dict[k] = _transliterate_arabic_chars(fields_dict[k])
        return fields_dict

    # Translate ALL location fields (dictionary pass)
    province = _translate_arabic(province_raw)
    city = _translate_arabic(city_raw)
    address1 = _translate_arabic(address1_raw)
    address2 = _translate_arabic(address2_raw)
    # Also handle the customer name
    name = _translate_arabic(name)
    # Batch Gemini call for any remaining Arabic across all fields
    _batched = _batch_transliterate({
        "name": name,
        "province": province,
        "city": city,
        "address1": address1,
        "address2": address2,
    })
    name = _batched["name"]
    province = _batched["province"]
    city = _batched["city"]
    address1 = _batched["address1"]
    address2 = _batched["address2"]
    address = (address1 + (" " + address2 if address2 else "")).strip()

    # 4. Map region: find a Region/Delegation matching ANY of the location
    # fields (province, city, address1, address2). All fields have already
    # been translated from Arabic above, so matching is purely text-based.
    region = None
    matched_delegation_name = None

    def _normalize(s):
        """Lowercase, strip accents, remove filler words & punctuation."""
        if not s:
            return ""
        s = unicodedata.normalize("NFKD", s)
        s = "".join(c for c in s if not unicodedata.combining(c))
        s = s.lower()
        for w in ("governorate", "gouvernorat", "gouvernement", "wilaya", "province"):
            s = s.replace(w, "")
        s = re.sub(r"[^a-z0-9 ]", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s

    def _levenshtein(a, b):
        if a == b:
            return 0
        if not a:
            return len(b)
        if not b:
            return len(a)
        prev_row = list(range(len(b) + 1))
        for i, ca in enumerate(a):
            cur_row = [i + 1]
            for j, cb in enumerate(b):
                cost = 0 if ca == cb else 1
                cur_row.append(min(cur_row[j] + 1, prev_row[j + 1] + 1, prev_row[j] + cost))
            prev_row = cur_row
        return prev_row[-1]

    # Collect all candidate texts (translated) — try province first since it's
    # the most reliable signal, then city, then addresses.
    candidate_texts = [t for t in (province, city, address1, address2) if t]
    candidate_texts_norm = [_normalize(t) for t in candidate_texts]

    all_regions = list(Region.objects.filter(is_active=True))
    from .models import Delegation
    all_delegations = list(Delegation.objects.filter(is_active=True).select_related("region"))

    # Strategy A: exact match on a Region name in any candidate
    for cand_norm in candidate_texts_norm:
        if not cand_norm:
            continue
        for r in all_regions:
            if _normalize(r.name) == cand_norm:
                region = r
                break
        if region:
            break

    # Strategy B: Region name contained in a candidate (or candidate contains Region)
    if not region:
        for cand_norm in candidate_texts_norm:
            if not cand_norm:
                continue
            for r in all_regions:
                r_norm = _normalize(r.name)
                if r_norm and (r_norm in cand_norm or cand_norm in r_norm):
                    region = r
                    break
            if region:
                break

    # Strategy C: exact match on a Delegation name in any candidate
    if not region:
        for cand_norm in candidate_texts_norm:
            if not cand_norm:
                continue
            for d_obj in all_delegations:
                if _normalize(d_obj.name) == cand_norm:
                    region = d_obj.region
                    matched_delegation_name = d_obj.name
                    break
            if region:
                break

    # Strategy D: Delegation name contained in a candidate (or vice versa)
    if not region:
        for cand_norm in candidate_texts_norm:
            if not cand_norm:
                continue
            for d_obj in all_delegations:
                d_norm = _normalize(d_obj.name)
                if d_norm and len(d_norm) >= 3 and (d_norm in cand_norm or cand_norm in d_norm):
                    region = d_obj.region
                    matched_delegation_name = d_obj.name
                    break
            if region:
                break

    # Strategy E: fuzzy match (typos) — try Regions first, then Delegations.
    # Checks ALL candidate fields (province, city, address1, address2), not just
    # province, because the governorate name sometimes lands in the city/address
    # field (e.g. city="Mednine" — a spelling variant of "Médenine").
    if not region and candidate_texts_norm:
        # Regions: best fuzzy match across every candidate field.
        best = None
        best_dist = 999
        for cand_norm in candidate_texts_norm:
            if not cand_norm:
                continue
            for r in all_regions:
                r_norm = _normalize(r.name)
                if not r_norm:
                    continue
                d = _levenshtein(cand_norm, r_norm)
                threshold = 2 if len(r_norm) <= 6 else 3
                if d <= threshold and d < best_dist:
                    best = r
                    best_dist = d
        if best:
            region = best
        else:
            # Delegations: best fuzzy match across every candidate field.
            best_dleg = None
            best_dist = 999
            for cand_norm in candidate_texts_norm:
                if not cand_norm:
                    continue
                for d_obj in all_delegations:
                    d_norm = _normalize(d_obj.name)
                    if not d_norm:
                        continue
                    dd = _levenshtein(cand_norm, d_norm)
                    threshold = 2 if len(d_norm) <= 6 else 3
                    if dd <= threshold and dd < best_dist:
                        best_dleg = d_obj
                        best_dist = dd
            if best_dleg:
                region = best_dleg.region
                matched_delegation_name = best_dleg.name

    # Strategy F: we have a region but no matched delegation yet (e.g. province
    # had the gov name directly, like "SFAX"). Look in the other fields for
    # a delegation that BELONGS to that region — that's likely the city.
    # E.g. province="SFAX" + address1="jbenyana" → find Delegation "Jebeniana"
    # in Sfax region (via fuzzy match) and use that as ville.
    if region and not matched_delegation_name and candidate_texts_norm:
        # Skip the first candidate (province) since we already used it for the region.
        # Look at city, address1, address2.
        region_delegations = [d for d in all_delegations if d.region_id == region.id]
        # First: exact / contained
        for cand_norm in candidate_texts_norm[1:]:
            if not cand_norm:
                continue
            for d_obj in region_delegations:
                d_norm = _normalize(d_obj.name)
                if not d_norm:
                    continue
                if d_norm == cand_norm or (len(d_norm) >= 3 and (d_norm in cand_norm or cand_norm in d_norm)):
                    matched_delegation_name = d_obj.name
                    break
            if matched_delegation_name:
                break
        # Then: fuzzy
        if not matched_delegation_name:
            best_match = None
            best_score = 999
            for cand_norm in candidate_texts_norm[1:]:
                if not cand_norm or len(cand_norm) < 3:
                    continue
                for d_obj in region_delegations:
                    d_norm = _normalize(d_obj.name)
                    if not d_norm or len(d_norm) < 3:
                        continue
                    dist = _levenshtein(cand_norm, d_norm)
                    threshold = 2 if len(d_norm) <= 6 else 3
                    if dist <= threshold and dist < best_score:
                        best_match = d_obj
                        best_score = dist
            if best_match:
                matched_delegation_name = best_match.name

    # Strategy G: AI fallback — invoked when:
    # - We don't have a clear region+ville (classic failed), OR
    # - Shopify province exists but matched region disagrees (mismatch).
    province_conflict = False
    if region and province:
        prov_norm = _normalize(province)
        reg_norm = _normalize(region.name)
        if prov_norm and reg_norm and prov_norm != reg_norm and prov_norm not in reg_norm and reg_norm not in prov_norm:
            province_conflict = True
    # Detect a "defaulted" delegation: the ville just took the governorate's
    # namesake delegation while the address field clearly names a DIFFERENT
    # locality (e.g. region=Ben Arous, ville=Ben Arous, but address="Hamamchat"
    # which is really Hammam Chatt). In that case let Gemini re-decide.
    delegation_defaulted = False
    if region and matched_delegation_name:
        try:
            md_norm = _normalize(matched_delegation_name)
            reg_norm2 = _normalize(region.name)
            # ville == governorate namesake?
            if md_norm and reg_norm2 and md_norm == reg_norm2:
                # Is there another locality in the address/city fields?
                for ct in candidate_texts_norm[1:]:
                    if ct and ct != md_norm and len(ct) >= 4:
                        delegation_defaulted = True
                        break
        except Exception:
            delegation_defaulted = False
    if not region or not matched_delegation_name or province_conflict or delegation_defaulted:
        try:
            # Build the catalog of options. Group delegations under their region.
            options_lines = []
            for r in all_regions:
                r_dlgs = [d for d in all_delegations if d.region_id == r.id]
                if r_dlgs:
                    options_lines.append(f"{r.name}: " + ", ".join(d.name for d in r_dlgs))
                else:
                    options_lines.append(r.name)
            # Only ask Gemini if we have data
            if options_lines:
                prompt = (
                    "Tu es assistant pour matcher une adresse Shopify à notre liste de gouvernorats (régions) et délégations (villes) tunisiens. "
                    "Choisis le couple REGION + VILLE qui correspond le mieux aux champs Shopify. "
                    "Tu DOIS choisir un nom EXACT de la liste fournie. "
                    "IMPORTANT : Les Tunisiens utilisent souvent des transcriptions avec chiffres (3=ع, 5=خ, 7=ح, 9=ق, 2=ء). "
                    "Exemples de transcriptions courantes :\n"
                    "  '9ar9na', 'gargana', 'karkenna' → Kerkennah\n"
                    "  '9afsa', 'gafsa' → Gafsa\n"
                    "  '3in dra7em' → Ain Draham\n"
                    "  'jbenyana', 'jebniana' → Jebeniana\n"
                    "  'nef6a', 'nafta' → Nefta\n"
                    "Si rien ne correspond clairement, réponds 'NONE'. "
                    "Réponds UNIQUEMENT au format : 'REGION: nom | VILLE: nom' ou 'NONE'.\n\n"
                    "Plus d'exemples :\n"
                    "  Champs 'SFAX / jbenyana' → 'REGION: Sfax | VILLE: Jebeniana'\n"
                    "  Champs 'Sfax / 9ar9na ramla' → 'REGION: Sfax | VILLE: Kerkennah'\n"
                    "  Champs 'Tozeur / Nefta' → 'REGION: Tozeur | VILLE: Nefta'\n\n"
                    f"Champs Shopify :\n"
                    f"  Province : {province}\n"
                    f"  City : {city}\n"
                    f"  Address1 : {address1}\n"
                    f"  Address2 : {address2}\n\n"
                    "Liste des régions et délégations :\n"
                    + "\n".join(options_lines)
                    + "\n\nRéponse :"
                )
                ai_response = _gemini_transliterate(prompt)
                try:
                    import logging
                    logging.getLogger(__name__).warning(
                        "Gemini region-match for province=%r city=%r address1=%r: response=%r",
                        province, city, address1, ai_response
                    )
                except Exception:
                    pass
                if ai_response and ai_response.strip().upper() != "NONE":
                    m = re.match(r"REGION:\s*(.+?)\s*\|\s*VILLE:\s*(.+)", ai_response.strip(), re.IGNORECASE)
                    if m:
                        region_name_ai = m.group(1).strip()
                        ville_name_ai = m.group(2).strip()
                        # Find region: try exact (case-insensitive), then fuzzy
                        region_match = None
                        for r in all_regions:
                            if r.name.lower() == region_name_ai.lower():
                                region_match = r
                                break
                        if not region_match:
                            # Fuzzy: closest by Levenshtein
                            best_dist = 999
                            for r in all_regions:
                                d_ = _levenshtein(_normalize(r.name), _normalize(region_name_ai))
                                if d_ < best_dist and d_ <= 3:
                                    region_match = r
                                    best_dist = d_
                        if region_match:
                            region = region_match
                            # Find delegation: exact, then fuzzy within this region
                            ville_match = None
                            for d in all_delegations:
                                if d.region_id == region_match.id and d.name.lower() == ville_name_ai.lower():
                                    ville_match = d
                                    break
                            if not ville_match:
                                # Fuzzy: closest within this region
                                best_dist = 999
                                v_norm = _normalize(ville_name_ai)
                                for d in all_delegations:
                                    if d.region_id != region_match.id:
                                        continue
                                    d_ = _levenshtein(_normalize(d.name), v_norm)
                                    if d_ < best_dist and d_ <= 3:
                                        ville_match = d
                                        best_dist = d_
                            if ville_match:
                                matched_delegation_name = ville_match.name
        except Exception:
            pass

    # If we matched a Delegation by name, replace the free-text city with the
    # canonical Delegation name so the team sees a clean value.
    if matched_delegation_name:
        city = matched_delegation_name
    elif region:
        # No precise delegation matched (customer gave no clean city, or only a
        # street). Avoid dumping messy street text into Ville — fall back to the
        # region name so Ville shows a clean, meaningful value (the full street
        # is still preserved in the address fields). The team can refine the
        # delegation from the dropdown if needed.
        city = region.name

    # 5. Get the SalesPage (or create it if missing)
    if sales_page_id is not None:
        # Explicit sales_page (e.g. Messenger DM routed by Facebook Page).
        sales_page = SalesPage.objects.filter(pk=sales_page_id).first()
        if not sales_page:
            sales_page = SalesPage.objects.filter(name__iexact="Barats").first()
    elif source == "converty":
        sales_page, created_page = SalesPage.objects.get_or_create(name="Converty", defaults={"is_active": True})
        # Ensure the Converty page exposes the same offers as the main page,
        # otherwise the order form shows "aucune offre attachée". Mirror the
        # main page's offers onto the Converty page (idempotent).
        try:
            from .models import Offer
            main_page = (SalesPage.objects.filter(name__iexact="Barats.tn").first()
                         or SalesPage.objects.exclude(name="Converty").filter(is_active=True).order_by("id").first())
            if main_page:
                for off in Offer.objects.filter(sales_pages=main_page):
                    off.sales_pages.add(sales_page)
        except Exception:
            pass
    else:
        sales_page = SalesPage.objects.filter(name__iexact="Barats.tn").first()
        if not sales_page:
            sales_page = SalesPage.objects.filter(is_active=True).order_by("id").first()
    if not sales_page:
        log_action(
            None, AuditLog.OTHER,
            description=f"Webhook Shopify #{shopify_order_number} : aucune SalesPage trouvée, commande ignorée.",
        )
        return JsonResponse({"status": "error", "message": "Aucune SalesPage configurée."}, status=500)

    # 6. Check for duplicate (webhooks get retried if no 200 is returned)
    if external_id:
        if source == "converty":
            existing = Order.objects.filter(converty_order_id=external_id).first()
        else:
            existing = Order.objects.filter(
                notes__contains=f"shopify_order_id={external_id}"
            ).first()
        if existing:
            return JsonResponse({"status": "ok", "message": "Already processed", "order_id": existing.id})

    # 7. Get-or-create the Customer
    customer, _ = Customer.objects.get_or_create(phone=phone_norm, defaults={"name": name})
    if name and customer.name != name:
        customer.name = name
        customer.save(update_fields=["name"])

    # 8. Create the Order draft
    line_items = payload.get("line_items") or []
    notes_parts = [
        f"{'converty_order_id' if source == 'converty' else 'shopify_order_id'}={shopify_order_id}",
        f"shopify_order_number={shopify_order_number}",
    ]
    customer_note = payload.get("note") or ""
    if customer_note:
        notes_parts.append(f"note_client: {customer_note}")

    from decimal import Decimal
    delivery_fee = Decimal("7")
    try:
        total_shipping = sum(
            Decimal(str(s.get("price") or "0"))
            for s in (payload.get("shipping_lines") or [])
        )
        if total_shipping > 0:
            delivery_fee = total_shipping
    except Exception:
        pass

    with transaction.atomic():
        order = Order.objects.create(
            customer=customer,
            sales_page=sales_page,
            region=region,
            ville=city,
            localite="",
            address=address,
            delivery_fee=delivery_fee,
            discount=Decimal("0"),
            notes=" | ".join(notes_parts),
            status=Order.NON_CONFIRMEE,
            customer_name=name,
            created_by=None,  # No user — webhook is unauthenticated
            source=({
                "converty": Order.SOURCE_CONVERTY,
                "messenger": Order.SOURCE_MESSENGER,
                "instagram": Order.SOURCE_INSTAGRAM,
                "shopify": Order.SOURCE_SHOPIFY,
            }.get(source, Order.SOURCE_SHOPIFY)),
            converty_order_id=(external_id if source == "converty" else ""),
        )

    # Helper: extract size from a Shopify line item by looking at variant_title
    # and line_item properties. Returns the matched size string or "".
    def _extract_variant(li, product, strict=False):
        """Pick the right ProductVariant from a Shopify line_item based on the
        color in variant_title. Falls back to the first variant if no match,
        UNLESS strict=True (used for bundles where a wrong fallback would create
        a fake order line with the wrong color).

        Searches across the whole product family (parent + V2/V3 children) so
        even if the parent has 'Gray' but the V2 sub-product doesn't, we still
        find it.
        """
        if not product:
            return None

        # Build family: root + all descendants (mirrors _extract_size).
        root = product
        while getattr(root, "parent_product", None):
            root = root.parent_product
        family = [root]
        try:
            from .models import Product as _Product
            for child in _Product.objects.filter(parent_product=root):
                family.append(child)
                for grand in _Product.objects.filter(parent_product=child):
                    family.append(grand)
        except Exception:
            pass

        # Collect all variants across the family. Track which variant belongs
        # to the specifically-requested product (preferred) vs family fallback.
        all_variants_self = list(product.variants.all())
        all_variants_family = []
        for p in family:
            for v in p.variants.all():
                all_variants_family.append(v)
        if not all_variants_family:
            return None

        # RULE: a product with exactly ONE colour/variant has no ambiguity —
        # whether or not Shopify sent a colour. Always use that single variant.
        # (Shopify omits the colour for single-colour products, e.g. Icy Maze
        # whose variant_title is just "S".)
        if len(all_variants_self) == 1:
            return all_variants_self[0]
        if len(all_variants_family) == 1:
            return all_variants_family[0]

        # Gather color candidates from variant_title and options
        candidates = []
        v_title = (li.get("variant_title") or "").strip()
        if v_title:
            for part in re.split(r"[/|\\,;]| - ", v_title):
                p = part.strip()
                if p:
                    candidates.append(p)
        # Some website/Shopify titles embed the variant in parentheses inside
        # the TITLE itself, e.g. "PULL SORA (BLACK, L)" or "Short Sora (Noir, L)".
        # Pull those tokens out as candidates too (split on comma/slash).
        li_title = (li.get("title") or li.get("name") or "")
        for paren in re.findall(r"\(([^)]*)\)", li_title):
            for part in re.split(r"[/|\\,;]| - ", paren):
                p = part.strip()
                if p:
                    candidates.append(p)
        for k in ("option1", "option2", "option3"):
            ov = li.get(k) or ""
            if ov:
                candidates.append(str(ov).strip())
        for prop in (li.get("properties") or []):
            pname = (prop.get("name") or "").strip().lower()
            pval = (prop.get("value") or "").strip()
            if pval and any(k in pname for k in ("color", "couleur", "colour")):
                candidates.append(pval)

        if not candidates:
            return None if strict else (all_variants_self[0] if all_variants_self else all_variants_family[0])

        # Build a list of FR/EN color synonyms so "Bleu" matches "Blue"/"BLUE",
        # "Noir" matches "Black"/"BLACK", "Gris" matches "Gray"/"Grey", etc.
        # The list is one-way: if a candidate equals one of the synonyms in a
        # group, we accept any variant whose color equals ANY synonym in the same group.
        color_aliases = [
            {"bleu", "blue", "azul"},
            {"noir", "black", "noire"},
            {"blanc", "white", "blanche"},
            {"rouge", "red", "rojo"},
            {"vert", "green", "verte"},
            {"gris", "gray", "grey", "grise"},
            {"jaune", "yellow"},
            {"orange", "naranja"},
            {"rose", "pink"},
            {"violet", "purple", "violet"},
            {"marron", "brown", "marrone", "café", "cafe"},
            {"beige", "cream", "crème", "creme"},
            {"camo", "camouflage"},
        ]

        def _synonyms_of(word):
            """Return the set of synonyms that contain this word (lowercased)."""
            w = word.strip().lower()
            for group in color_aliases:
                if w in group:
                    return group
            return {w}

        # Try to match each candidate against color_label and color_name.
        # Two passes: first the product's own variants, then the family.
        for variant_pool in (all_variants_self, all_variants_family):
            for cand in candidates:
                cand_syns = _synonyms_of(cand)
                for v in variant_pool:
                    lbl = (v.color_label or "").strip().lower()
                    nm = (v.color_name or "").strip().lower()
                    if lbl in cand_syns or nm in cand_syns:
                        return v
            # Partial contains match (handles "Gris foncé" matches "Gris")
            for cand in candidates:
                cl = cand.lower()
                for v in variant_pool:
                    lbl = (v.color_label or "").strip().lower()
                    nm = (v.color_name or "").strip().lower()
                    if lbl and (lbl in cl or cl in lbl):
                        return v
                    if nm and (nm in cl or cl in nm):
                        return v

        # No color matched any variant of this product (or its family).
        # - In strict mode (bundles): return None so the team picks manually.
        # - In normal mode: fall back to first variant of the product itself.
        if strict:
            return None
        return all_variants_self[0] if all_variants_self else all_variants_family[0]

    def _extract_size(li, product):
        # Shopify variant_title format examples:
        #   "Blanc / XL"
        #   "L / Bleu"
        #   "Taille XL"
        #   "39"  (just the number)
        candidates = []
        v_title = (li.get("variant_title") or "").strip()
        if v_title:
            # Split by common separators
            for part in re.split(r"[/|\\,;]| - ", v_title):
                p = part.strip()
                if p:
                    candidates.append(p)
        # Also look in 'properties' (custom fields the merchant set up)
        for prop in (li.get("properties") or []):
            pname = (prop.get("name") or "").strip().lower()
            pval = (prop.get("value") or "").strip()
            if pval and any(k in pname for k in ("size", "taille", "pointure")):
                candidates.append(pval)
        # Also look at the variant's "option" fields (Shopify product variant)
        # which may come through differently
        for k in ("option1", "option2", "option3"):
            ov = li.get(k) or ""
            if ov:
                candidates.append(str(ov).strip())

        if not candidates:
            return ""

        # Collect known sizes for this product AND its family (parent + V2/V3 children)
        # across all variants. This is important because the merchant may have
        # "Pull WaveLine" (sizes 1-4) and "Pull WaveLine V2" (sizes 2-5) and a
        # customer who picks 2XL should land on size "5" no matter which sub-product matched.
        known_sizes = set()
        if product:
            # Find the root: walk up parent_product until None
            root = product
            while getattr(root, "parent_product", None):
                root = root.parent_product
            # Build family = root + all descendants
            family = [root]
            try:
                from .models import Product as _Product
                # children of root (V2)
                for child in _Product.objects.filter(parent_product=root):
                    family.append(child)
                    # grandchildren (V3)
                    for grand in _Product.objects.filter(parent_product=child):
                        family.append(grand)
            except Exception:
                pass
            for p in family:
                for v in p.variants.all():
                    for u in v.units.all():
                        if u.size:
                            known_sizes.add(u.size.strip())

        # LETTER → NUMBER mapping for Tunisian size convention:
        #   1=S, 2=M, 3=L, 4=XL, 5=XXL
        # Plus a special rule: if size "1" doesn't exist in stock, "S" falls back to "2".
        letter_to_number = {
            "xs": "1", "s": "1",
            "m": "2",
            "l": "3",
            "xl": "4",
            "xxl": "5", "2xl": "5",
            "xxxl": "5", "3xl": "5",  # treat as XXL since we only go up to 5
        }
        # Reverse direction (rarely used here but available)
        # number_to_letter = {"1": "S", "2": "M", "3": "L", "4": "XL", "5": "XXL"}

        if known_sizes:
            # 1. Direct match (case insensitive) — handles cases where the
            # product genuinely uses S/M/L sizes
            for cand in candidates:
                for ks in known_sizes:
                    if cand.lower() == ks.lower():
                        return ks  # return the canonical capitalization

            # 2. Letter → number conversion (the common Tunisian case)
            for cand in candidates:
                num = letter_to_number.get(cand.lower())
                if num is None:
                    continue
                if num in known_sizes:
                    return num
                # SPECIAL RULE: if S (= "1") is not stocked, fall back to M ("2")
                if num == "1" and "2" in known_sizes:
                    return "2"

            # 3. Number → letter conversion (less common but possible)
            number_to_letter = {"1": "S", "2": "M", "3": "L", "4": "XL", "5": "XXL"}
            for cand in candidates:
                ltr = number_to_letter.get(cand.strip())
                if ltr and ltr in known_sizes:
                    return ltr

        # 4. No known sizes or no match — fallback: regex-look-like-a-size
        for cand in candidates:
            if re.match(r"^(xs|s|m|l|xl|xxl|xxxl|3xl|4xl|\d{1,2})$", cand.lower()):
                return cand
        return ""

    import re

    # 9. Map each line_item: first try matching to an Offer (bundle), then to a Product.
    # If we can't find one, we still record the line as a note for the team.
    from .models import Offer, OfferProduct, OrderOffer
    unmatched_items = []
    for li in line_items:
        title = (li.get("title") or li.get("name") or "").strip()
        variant_title = (li.get("variant_title") or "").strip()
        quantity = int(li.get("quantity") or 1)
        unit_price = Decimal(str(li.get("price") or "0"))

        if not title:
            continue

        title_lower = title.lower()

        # --- (A) Try to match an OFFER (bundle) by name first ---
        offer = Offer.objects.filter(name__iexact=title, is_active=True).first()
        if not offer:
            # Normalized match: ignore stray trailing/leading punctuation and
            # extra spaces, so an offer named "Ensemble 3pcs SORA ." still
            # matches a website title "Ensemble 3pcs sora".
            def _norm_name(s):
                s = (s or "").lower().strip()
                s = re.sub(r"\s+", " ", s)
                return s.strip(" .,-_/|;:·•").strip()
            tnorm = _norm_name(title)
            if tnorm:
                for o in Offer.objects.filter(is_active=True):
                    if _norm_name(o.name) == tnorm:
                        offer = o
                        break
        if not offer:
            # Try "offer name contained in shopify title"
            candidates = []
            for o in Offer.objects.filter(is_active=True):
                o_name_lower = (o.name or "").strip().lower()
                if o_name_lower and o_name_lower in title_lower:
                    candidates.append(o)
            if candidates:
                # Prefer longest match
                offer = max(candidates, key=lambda o: len(o.name))

        # --- (B) ALSO try to match a PRODUCT (so we have both candidates) ---
        product = Product.objects.filter(name__iexact=title).first()
        if not product:
            candidates = []
            for p in Product.objects.all():
                p_name_lower = (p.name or "").strip().lower()
                if p_name_lower and p_name_lower in title_lower:
                    candidates.append(p)
            if candidates:
                product = max(candidates, key=lambda p: len(p.name))
        if not product and variant_title:
            full_title = f"{title} {variant_title}".lower()
            for p in Product.objects.all():
                p_name_lower = (p.name or "").strip().lower()
                if p_name_lower and p_name_lower in full_title:
                    product = p
                    break

        # Word-overlap fuzzy match (handles title shorter than catalog name,
        # e.g. "Polo Ralph Kaja" vs "Polo Ralph Kaja Summer"). Pick the product
        # sharing the most consecutive leading words with the title.
        if not product:
            t_words = title_lower.split()
            best = None; best_score = 0
            for p in Product.objects.all():
                pn = (p.name or "").strip().lower().split()
                if not pn:
                    continue
                # count leading matching words
                k = 0
                for a, b in zip(t_words, pn):
                    if a == b:
                        k += 1
                    else:
                        break
                # require at least 2 leading words in common and most of the
                # shorter name to match, to avoid loose matches.
                if k >= 2 and k >= min(len(t_words), len(pn)) - 1 and k > best_score:
                    best = p; best_score = k
            if best:
                product = best

        # --- (C) Ask Gemini to validate the best choice between offer / product / nothing ---
        # This handles cases like Shopify title "Pull Polo Crochet Blueline" where
        # the classic substring match might wrongly pick an Offer named "Pull Polo".
        # Only invoke when at least one candidate was found OR no clear exact match.
        gemini_pick = None
        try:
            all_offers = list(Offer.objects.filter(is_active=True))
            all_products = list(Product.objects.all())
            options_lines = []
            for o in all_offers:
                if o.name:
                    options_lines.append(f"OFFRE: {o.name}")
            for p in all_products:
                if p.name:
                    options_lines.append(f"PRODUIT: {p.name}")
            if options_lines:
                # Run the AI matcher whenever there isn't a clean exact match —
                # including when the classic substring search found NOTHING
                # (e.g. catalog "Ensemble ICY MAZE" vs Converty "Ensemble ice
                # maze"; substring fails but the AI can bridge icy/ice).
                exact_offer_match = bool(Offer.objects.filter(name__iexact=title, is_active=True).first())
                exact_product_match = bool(Product.objects.filter(name__iexact=title).first())
                if not (exact_offer_match or exact_product_match):
                    prompt = (
                        "Tu es assistant pour matcher un titre de commande Shopify à notre catalogue.\n"
                        "Notre catalogue a deux types : OFFRE (pack/ensemble de produits) et PRODUIT (article seul).\n\n"
                        "RÈGLE PRIMORDIALE : le PREMIER MOT du titre Shopify détermine le type d'article.\n"
                        "  - Titre commence par 'Pull' → cherche 'Pull X' dans le catalogue (OFFRE ou PRODUIT)\n"
                        "  - Titre commence par 'Polo' → cherche 'Polo X'\n"
                        "  - Titre commence par 'Pants' → cherche 'Pants X'\n"
                        "  - Titre commence par 'Veste' → cherche 'Veste X'\n"
                        "  - Titre commence par 'Ensemble' ou 'Tenue' → cherche 'Ensemble X' ou 'Tenue X'\n"
                        "JAMAIS un 'Pull X' ne doit matcher un 'Ensemble X' ou inversement.\n\n"
                        "Si rien ne correspond clairement, réponds 'NONE'.\n"
                        "Réponds UNIQUEMENT par : 'OFFRE: nom' ou 'PRODUIT: nom' ou 'NONE'.\n\n"
                        "Exemples critiques :\n"
                        "  'Pull Camo ZR' → 'OFFRE: Pull Camo' (PAS 'Ensemble Camo ZR' — c'est un Pull seul, pas un ensemble)\n"
                        "  'Pull Polo Crochet Blueline' → 'OFFRE: Pull BlueLine' (PAS 'Ensemble Blueline')\n"
                        "  'Ensemble Camo ZR' → 'OFFRE: Ensemble Camo ZR' (commence par Ensemble)\n"
                        "  'Pull Vintage' → 'PRODUIT: PULL VINTAGE'\n\n"
                        f"Titre Shopify : {title}\n"
                        f"Variante : {variant_title or '(aucune)'}\n\n"
                        "Catalogue :\n"
                        + "\n".join(options_lines)
                        + "\n\nRéponse :"
                    )
                    ai_response = _gemini_transliterate(prompt)
                    # Log what Gemini returned for debugging
                    try:
                        import logging
                        logging.getLogger(__name__).warning(
                            "Gemini product-match for title=%r: response=%r",
                            title, ai_response
                        )
                    except Exception:
                        pass
                    if ai_response:
                        ai_response = ai_response.strip().strip('"').strip("'")
                        # Tolerate response without prefix (e.g. just "Pull Polo")
                        # Try OFFRE/PRODUIT prefix first, then fall back to fuzzy lookup
                        upper = ai_response.upper()
                        if upper == "NONE":
                            offer = None
                            product = None
                        elif upper.startswith("OFFRE:"):
                            target_name = ai_response[6:].strip()
                            tn = target_name.lower()
                            for o in all_offers:
                                if o.name and o.name.strip().lower() == tn:
                                    offer = o; product = None; gemini_pick = "offer"; break
                            if not gemini_pick and tn:
                                cands = [o for o in all_offers if o.name and (
                                    o.name.strip().lower().startswith(tn) or tn.startswith(o.name.strip().lower())
                                    or tn in o.name.strip().lower() or o.name.strip().lower() in tn)]
                                if cands:
                                    offer = min(cands, key=lambda o: abs(len(o.name) - len(target_name)))
                                    product = None; gemini_pick = "offer_fuzzy"
                        elif upper.startswith("PRODUIT:"):
                            target_name = ai_response[8:].strip()
                            tn = target_name.lower()
                            # 1) exact match
                            for p in all_products:
                                if p.name and p.name.strip().lower() == tn:
                                    product = p; offer = None; gemini_pick = "product"; break
                            # 2) fuzzy: catalog name starts-with / contains Gemini's
                            #    answer, or vice-versa (handles "Polo Ralph Kaja" vs
                            #    "Polo Ralph Kaja Summer").
                            if not gemini_pick and tn:
                                cands = []
                                for p in all_products:
                                    pn = (p.name or "").strip().lower()
                                    if not pn:
                                        continue
                                    if pn.startswith(tn) or tn.startswith(pn) or tn in pn or pn in tn:
                                        cands.append(p)
                                if cands:
                                    # prefer the closest length to Gemini's answer
                                    product = min(cands, key=lambda p: abs(len(p.name) - len(target_name)))
                                    offer = None; gemini_pick = "product_fuzzy"
                        else:
                            # Gemini answered without prefix — search the name in both lists
                            name_lower = ai_response.lower()
                            found = False
                            for p in all_products:
                                if p.name and p.name.strip().lower() == name_lower:
                                    product = p
                                    offer = None
                                    gemini_pick = "product_no_prefix"
                                    found = True
                                    break
                            if not found:
                                for o in all_offers:
                                    if o.name and o.name.strip().lower() == name_lower:
                                        offer = o
                                        product = None
                                        gemini_pick = "offer_no_prefix"
                                        break
        except Exception:
            # If Gemini fails for any reason, fall back to the classic match
            pass

        # Safety guard: if Gemini picked an OFFRE/PRODUIT whose first word
        # mismatches the Shopify title's first word (e.g. 'Pull Camo ZR' →
        # 'Ensemble Camo ZR'), reject Gemini's pick to avoid wrong matching.
        # Common boundary: 'Pull'/'Polo'/'Pants'/'Veste'/'Shirt'/'Short' should NOT
        # become 'Ensemble' or 'Tenue', and vice versa.
        try:
            single_keywords = {"pull", "polo", "pants", "pant", "veste", "shirt", "short", "doudoune", "gillet", "5 pieces"}
            bundle_keywords = {"ensemble", "tenue"}
            title_first = title.strip().lower().split()[0] if title.strip() else ""
            picked_obj = offer or product
            picked_name = (picked_obj.name if picked_obj else "")
            picked_first = picked_name.strip().lower().split()[0] if picked_name else ""
            title_is_single = title_first in single_keywords
            title_is_bundle = title_first in bundle_keywords
            picked_is_single = picked_first in single_keywords
            picked_is_bundle = picked_first in bundle_keywords
            # Does the picked name share any MEANINGFUL word with the title
            # (beyond the generic type keyword like 'pull'/'ensemble')? If not,
            # a same-type pick (e.g. 'pull ami' → 'Pull Vintage') is a bad guess.
            _generic = single_keywords | bundle_keywords
            _tw = set(title.lower().split())
            _pw = set(picked_name.lower().split())
            _shared_meaningful = (_tw & _pw) - _generic
            _same_type_bad = (
                picked_obj is not None
                and ((title_is_single and picked_is_single) or (title_is_bundle and picked_is_bundle))
                and not _shared_meaningful
                # only when the title itself has a distinctive word to match on
                and bool(_tw - _generic)
            )
            if (title_is_single and picked_is_bundle) or (title_is_bundle and picked_is_single) or _same_type_bad:
                # Reject Gemini pick — it crossed the boundary
                try:
                    import logging
                    logging.getLogger(__name__).warning(
                        "Gemini cross-boundary pick REJECTED: title=%r → pick=%r (type mismatch)",
                        title, picked_name
                    )
                except Exception:
                    pass
                # Try to find a same-type alternative
                title_words = set(title.lower().split())
                alt_found = False
                if title_is_single:
                    # Score each candidate by how many words from the title
                    # overlap — but the shared word must be MORE than the generic
                    # type keyword (pull/ensemble/claquette...). Sharing only
                    # 'pull' is not a real match (e.g. 'pull ami' vs 'Pull
                    # Vintage'): leave the line unmatched rather than guess wrong.
                    generic_kw = single_keywords | bundle_keywords
                    best_score = 0
                    best_offer = None
                    best_product = None
                    for o in all_offers:
                        if not o.name:
                            continue
                        o_first = o.name.strip().lower().split()[0]
                        if o_first not in single_keywords:
                            continue
                        o_words = set(o.name.lower().split())
                        common = (o_words & title_words) - generic_kw
                        score = len(common)  # only NON-generic shared words
                        if score > best_score:
                            best_score = score
                            best_offer = o
                            best_product = None
                    for p in all_products:
                        if not p.name:
                            continue
                        p_first = p.name.strip().lower().split()[0]
                        if p_first not in single_keywords:
                            continue
                        p_words = set(p.name.lower().split())
                        common = (p_words & title_words) - generic_kw
                        score = len(common)
                        if score > best_score:
                            best_score = score
                            best_product = p
                            best_offer = None
                    # Require at least one meaningful (non-generic) shared word.
                    if best_score >= 1 and best_offer:
                        offer = best_offer
                        product = None
                        gemini_pick = "guard_fix_offer"
                        alt_found = True
                    elif best_score >= 1 and best_product:
                        product = best_product
                        offer = None
                        gemini_pick = "guard_fix_product"
                        alt_found = True
                if not alt_found:
                    # No safer alternative — drop the bad pick
                    offer = None
                    product = None
        except Exception:
            pass

        # --- (D) Process the final pick ---
        if offer:
            # Found a bundle. An offer ordered with quantity N should come in as
            # N SEPARATE offer instances (e.g. "Pull Camo" + "Pull Camo"), each
            # its own priced bundle — NOT one OrderOffer with quantity N (which
            # mis-prints and mis-prices). So we loop N times, creating one
            # OrderOffer (qty 1) per unit ordered.
            offer_products = list(offer.products.all())
            # --- resolve colours/sizes ONCE (same for every copy) ---
            v_title_full = (li.get("variant_title") or "").strip()
            parts = [p.strip() for p in v_title_full.split("/") if p.strip()]
            import re as _re_local
            def _looks_like_size(tok):
                t = (tok or "").strip().lower()
                if not t:
                    return False
                return bool(_re_local.match(r"^(xs|s|m|l|xl|xxl|xxxl|2xl|3xl|4xl|\d{1,2})$", t))
            color_parts = [p for p in parts if not _looks_like_size(p)]
            size_parts  = [p for p in parts if _looks_like_size(p)]
            _li_title_for_color = (li.get("title") or li.get("name") or "")
            for paren in _re_local.findall(r"\(([^)]*)\)", _li_title_for_color):
                for tok in _re_local.split(r"[/|\\,;]| - ", paren):
                    tok = tok.strip()
                    if not tok:
                        continue
                    if _looks_like_size(tok):
                        size_parts.append(tok)
                    elif tok not in color_parts:
                        color_parts.append(tok)
            shared_size_hint = size_parts[-1] if size_parts else ""

            assignments = [None] * len(offer_products)
            if len(color_parts) == len(offer_products) and len(offer_products) > 1:
                for i, op in enumerate(offer_products):
                    cand = color_parts[i]
                    synthetic_li_test = dict(li)
                    synthetic_li_test["variant_title"] = cand
                    if _extract_variant(synthetic_li_test, op.product, strict=True) is not None:
                        assignments[i] = cand
            for i, op in enumerate(offer_products):
                if assignments[i] is not None:
                    continue
                synthetic_li_test = dict(li)
                for cand in color_parts:
                    synthetic_li_test["variant_title"] = cand
                    match = _extract_variant(synthetic_li_test, op.product, strict=True)
                    if match is not None:
                        assignments[i] = cand
                        break

            # Pre-compute each sub-product's variant + size once.
            sub_resolved = []  # list of (op, variant_guess, size_guess)
            for i, op in enumerate(offer_products):
                color_for_this = assignments[i] or ""
                synthetic_title = (color_for_this + "/" + shared_size_hint) if shared_size_hint else color_for_this
                synthetic_li = dict(li)
                synthetic_li["variant_title"] = synthetic_title
                size_guess = _extract_size(synthetic_li, op.product)
                variant_guess = _extract_variant(synthetic_li, op.product, strict=True)
                if variant_guess is None:
                    sub_variants = list(op.product.variants.all())
                    if len(sub_variants) == 1:
                        variant_guess = sub_variants[0]
                    else:
                        variant_guess = _extract_variant(synthetic_li, op.product, strict=False)
                if not size_guess and shared_size_hint:
                    synthetic_li2 = dict(li)
                    synthetic_li2["variant_title"] = shared_size_hint
                    size_guess = _extract_size(synthetic_li2, op.product)
                sub_resolved.append((op, variant_guess, size_guess))

            # Create ONE OrderOffer per unit ordered (quantity copies).
            for _copy in range(max(quantity, 1)):
                order_offer = OrderOffer.objects.create(
                    order=order, offer=offer,
                    offer_name=offer.name,
                    bundle_price=offer.price_for_page(order.sales_page),
                    quantity=1,
                )
                for op, variant_guess, size_guess in sub_resolved:
                    OrderLine.objects.create(
                        order=order,
                        order_offer=order_offer,
                        product=op.product,
                        variant=variant_guess,
                        size=size_guess,
                        quantity=op.quantity,
                        unit_price=0,
                    )
            continue  # Done with this line_item

        # --- (E) Process as simple product ---
        if not product:
            unmatched_items.append(f"{title} (qté {quantity})")
            continue

        variant = _extract_variant(li, product)
        size_guess = _extract_size(li, product)
        OrderLine.objects.create(
            order=order,
            product=product,
            variant=variant,
            size=size_guess,
            quantity=quantity,
            unit_price=unit_price,
        )

    # End of `for li in line_items:` loop

    # 10. If there are unmatched items, add them to the notes
    if unmatched_items:
        order.notes += " | ARTICLES NON RECONNUS: " + "; ".join(unmatched_items)
        order.save(update_fields=["notes"])

    # 11. Recompute total — refresh from DB first to bust any cached
    # relations from earlier (otherwise the freshly-created OrderOffer might
    # not appear in self.order_offers.all()).
    order.refresh_from_db()
    # Shopify free-shipping offer: if the articles subtotal (excluding delivery,
    # before discount) is 150 DT or more, shipping is free. Shopify orders only.
    if source == "shopify":
        try:
            offers_sum = sum((oo.offer_total for oo in order.order_offers.all()), Decimal("0"))
            lines_sum = sum((l.line_total for l in order.lines.filter(order_offer__isnull=True)), Decimal("0"))
            if (offers_sum + lines_sum) >= Decimal("150"):
                order.delivery_fee = Decimal("0")
                order.save(update_fields=["delivery_fee"])
        except Exception:
            pass
    order.recalc_total()

    # 12. Audit log — put line_items first so they're not truncated.
    audit_extra = "LINE_ITEMS=" + str(line_items) + " | PAYLOAD=" + str(payload)
    log_action(
        None, AuditLog.CREATE,
        description=(
            f"Commande Shopify #{shopify_order_number} reçue → Order #{order.id} créée "
            f"({len(line_items)} ligne(s), {len(unmatched_items)} non reconnue(s))"
        ),
        target_model="Order", target_id=order.id,
        extra=audit_extra[:50000],
    )

    # --- Customer SMS: order received (today/tomorrow by Tunisia time) ---
    # For website / Converty / Messenger DM orders. Fires once per order.
    if source in ("shopify", "converty", "messenger") and not order.sms_created_sent:
        try:
            from . import sms_service
            local_now = timezone.localtime(timezone.now())
            cutoff_min = 16 * 60 + 30   # 16:30
            when_today = (local_now.hour * 60 + local_now.minute) < cutoff_min
            ok, _info = sms_service.send_sms(
                order.customer.phone if order.customer else "",
                sms_service.msg_created(when_today),
            )
            if ok:
                order.sms_created_sent = True
                order.save(update_fields=["sms_created_sent"])
        except Exception:
            pass

    return JsonResponse({
        "status": "ok",
        "order_id": order.id,
        "unmatched": unmatched_items,
    })


# ---- Admin tools page (superuser only) -------------------------------------

@login_required(login_url="/login/")
def api_debug_navex_etat(request):
    """Debug-only: try multiple Navex API parameter variations to find which
    one returns the exchange return barcode. Tries with/without include_echange,
    different parameter formats, GET and POST.
    """
    if not request.user.is_superuser:
        return JsonResponse({"status": "error", "message": "Accès refusé."}, status=403)
    bordereau = (request.GET.get("bordereau") or "").strip()
    if not bordereau:
        return JsonResponse({"status": "error", "message": "Bordereau requis."}, status=400)
    import urllib.request, urllib.parse
    token = os.environ.get("NAVEX_API_TOKEN", "")
    if not token:
        return JsonResponse({"status": "error", "message": "Token manquant."}, status=500)

    url = f"https://app.navex.tn/api/rashop-etat-{token}/v1/post.php"

    variations = [
        ("POST code=X (singular) + include_echange=1", {"code": bordereau, "include_echange": "1"}, "POST"),
        ("POST code=X (singular) without include", {"code": bordereau}, "POST"),
        ("POST codes=X (plural) + include_echange=1", {"codes": bordereau, "include_echange": "1"}, "POST"),
        ("POST codes=X (plural) without include", {"codes": bordereau}, "POST"),
        ("POST code=X + include-echange=1 (hyphen)", {"code": bordereau, "include-echange": "1"}, "POST"),
        ("GET code=X + include_echange=1", {"code": bordereau, "include_echange": "1"}, "GET"),
    ]

    results = []
    for label, params, method in variations:
        try:
            if method == "POST":
                body = urllib.parse.urlencode(params).encode("utf-8")
                req = urllib.request.Request(url, data=body, method="POST")
                req.add_header("Content-Type", "application/x-www-form-urlencoded")
            else:
                full_url = url + "?" + urllib.parse.urlencode(params)
                req = urllib.request.Request(full_url, method="GET")
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    data = {"_not_json": raw[:500]}
        except Exception as e:
            data = {"_error": str(e)}
        results.append({"variation": label, "params": params, "method": method, "response": data})

    return JsonResponse({
        "status": "ok",
        "bordereau": bordereau,
        "results": results,
    }, json_dumps_params={"indent": 2, "ensure_ascii": False})


@login_required(login_url="/login/")
def admin_tools(request):
    """Maintenance/repair page with one-click action buttons.
    Only accessible to superusers."""
    if not request.user.is_superuser:
        return redirect("home")
    from .models import ConvertyConnection
    converty_conn = ConvertyConnection.objects.filter(is_active=True).order_by("-updated_at").first()
    return render(request, "inventory/admin_tools.html", {"converty_conn": converty_conn})


@csrf_exempt
@require_POST
@login_required(login_url="/login/")
def api_admin_run_tool(request, tool_name):
    """Run a maintenance tool by name. Returns the captured output as JSON."""
    if not request.user.is_superuser:
        return JsonResponse({"status": "error", "message": "Accès refusé."}, status=403)
    import io
    from django.core.management import call_command

    # Whitelist of allowed tools — never call arbitrary commands
    ALLOWED = {
        "fix_supprime_navex_orders_dryrun": ("fix_supprime_navex_orders", []),
        "fix_supprime_navex_orders_apply":  ("fix_supprime_navex_orders", ["--apply"]),
        "fix_livree_orders_dryrun":         ("fix_livree_orders", []),
        "fix_livree_orders_apply":          ("fix_livree_orders", ["--apply"]),
        "recalc_order_totals":              ("recalc_order_totals", []),
    }
    if tool_name not in ALLOWED:
        return JsonResponse({"status": "error", "message": f"Outil inconnu : {tool_name}"}, status=400)
    cmd, cmd_args = ALLOWED[tool_name]
    out = io.StringIO()
    err = io.StringIO()
    try:
        call_command(cmd, *cmd_args, stdout=out, stderr=err)
        return JsonResponse({
            "status": "ok",
            "tool": tool_name,
            "output": out.getvalue(),
            "errors": err.getvalue(),
        })
    except Exception as e:
        return JsonResponse({
            "status": "error",
            "message": str(e),
            "output": out.getvalue(),
            "errors": err.getvalue(),
        }, status=500)


# ---- Regions / Delegations (cascaded dropdown) -----------------------------

@login_required(login_url="/login/")
def api_region_delegations(request, region_id):
    """Return the list of delegations (sub-zones) for a given governorate.
    Used by the frontend to populate a cascaded dropdown."""
    from .models import Region, Delegation
    if not _orders_role_check(request):
        return JsonResponse({"status": "error"}, status=403)
    try:
        region = Region.objects.get(pk=region_id)
    except Region.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Gouvernorat introuvable."}, status=404)
    delegations = Delegation.objects.filter(region=region, is_active=True).order_by("name")
    return JsonResponse({
        "status": "ok",
        "region": {"id": region.id, "name": region.name},
        "delegations": [{"id": d.id, "name": d.name} for d in delegations],
    })


@login_required(login_url="/login/")
def api_all_delegations(request):
    """Return ALL regions and their delegations in a single payload.
    Used by the unified searchable dropdown.
    """
    from .models import Region, Delegation
    if not _orders_role_check(request):
        return JsonResponse({"status": "error"}, status=403)
    regions = Region.objects.filter(is_active=True).order_by("name").prefetch_related("delegations")
    out = []
    for r in regions:
        out.append({
            "id": r.id,
            "name": r.name,
            "delegations": [
                {"id": d.id, "name": d.name}
                for d in r.delegations.filter(is_active=True).order_by("name")
            ],
        })
    return JsonResponse({"status": "ok", "regions": out})


# ---- User theme preference (light/dark mode) -------------------------------

@csrf_exempt
@require_POST
@login_required(login_url="/login/")
def api_user_theme(request):
    """Update the logged-in user's theme preference (dark/light)."""
    from .models import UserProfile
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"status": "error"}, status=400)
    theme = (data.get("theme") or "").strip().lower()
    if theme not in (UserProfile.THEME_DARK, UserProfile.THEME_LIGHT):
        return JsonResponse({"status": "error", "message": "Theme invalide."}, status=400)
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    profile.theme = theme
    profile.save(update_fields=["theme"])
    return JsonResponse({"status": "ok", "theme": theme})


# ---- Admin-only: manage offers ---------------------------------------------

def _admin_only(view_fn):
    from functools import wraps
    @wraps(view_fn)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("login")
        if not request.user.is_superuser:
            return redirect("home")
        return view_fn(request, *args, **kwargs)
    return wrapper


def _admin_or_office(view_fn):
    """Allow superusers and office-role staff to manage offers."""
    from functools import wraps
    @wraps(view_fn)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("login")
        if request.user.is_superuser:
            return view_fn(request, *args, **kwargs)
        try:
            role = request.user.profile.role
        except Exception:
            role = "office"
        if role == "office":
            return view_fn(request, *args, **kwargs)
        return redirect("home")
    return wrapper


@_admin_or_office
def price_change_page(request):
    """Office page: search a client by phone, see their eligible orders, and set
    a new total (reduction agreed by phone with Navex) so 'notre total' matches
    what Navex will collect — avoiding price-mismatch flags in the sync."""
    from .models import Order, Customer
    phone = (request.GET.get("phone") or "").strip()
    orders = []
    searched = bool(phone)
    if phone:
        phone_digits = "".join(c for c in phone if c.isdigit())
        customers = Customer.objects.filter(
            Q(phone__icontains=phone_digits) | Q(phone2__icontains=phone_digits)
        )
        eligible_statuses = [Order.CONFIRMEE, Order.EN_COURS, Order.AU_MAGASIN]
        qs = (Order.objects.filter(customer__in=customers)
              .exclude(bordereau_barcode="")
              .filter(status__in=eligible_statuses)
              .select_related("customer", "sales_page")
              .order_by("-created_at"))
        orders = list(qs)
    return render(request, "inventory/price_change.html", {
        "phone": phone,
        "orders": orders,
        "searched": searched,
    })


@_admin_or_office
def api_set_order_price(request, pk):
    """Set a manual price override on an order (office reduction)."""
    from .models import Order, log_action, AuditLog
    from decimal import Decimal, InvalidOperation
    if request.method != "POST":
        return JsonResponse({"status": "error", "message": "POST requis."}, status=405)
    try:
        data = json.loads(request.body or "{}")
    except Exception:
        data = {}
    try:
        new_price = Decimal(str(data.get("price", "")).replace(",", "."))
    except (InvalidOperation, ValueError):
        return JsonResponse({"status": "error", "message": "Prix invalide."}, status=400)
    if new_price < 0:
        return JsonResponse({"status": "error", "message": "Prix invalide."}, status=400)
    order = get_object_or_404(Order, pk=pk)
    if order.status in (Order.LIVREE, Order.PAYEE, Order.RETURNED, Order.RETURNING, Order.ANNULEE):
        return JsonResponse({"status": "error", "message": "Commande déjà livrée/terminée — prix non modifiable."}, status=400)
    old_total = order.total
    order.price_override = new_price
    order.save(update_fields=["price_override"])
    order.recalc_total()  # applies the override -> total = new_price
    for so in order.shipping_orders.all():
        so.amount_collected = new_price
        so.save(update_fields=["amount_collected"])
    try:
        log_action(
            request.user, AuditLog.OTHER,
            description=f"Changement de prix commande #{order.id} : {old_total} → {new_price} DT",
            target_model="Order", target_id=order.id, request=request,
        )
    except Exception:
        pass
    return JsonResponse({"status": "ok", "new_total": str(order.total)})


@_admin_or_office
def offers_manage(request):
    """Custom admin page to manage offers, with an optional page filter."""
    from .models import Offer, SalesPage, Product
    page_filter = request.GET.get("page", "all")  # 'all', 'barats', 'converty', or a page id
    offers = Offer.objects.prefetch_related("sales_pages", "products__product").all()
    if page_filter == "barats":
        offers = offers.filter(sales_pages__name__iexact="Barats.tn").distinct()
    elif page_filter == "converty":
        offers = offers.filter(sales_pages__name__iexact="Converty").distinct()
    elif page_filter not in ("all", ""):
        try:
            offers = offers.filter(sales_pages__id=int(page_filter)).distinct()
        except ValueError:
            pass
    return render(request, "inventory/offers_manage.html", {
        "offers": offers,
        "sales_pages": SalesPage.objects.filter(is_active=True),
        "products": Product.objects.filter(archived=False).order_by("name"),
        "page_filter": page_filter,
    })


@csrf_exempt
@require_POST
@_admin_or_office
def api_offer_create(request):
    from .models import Offer, SalesPage, Product, OfferProduct, log_action, AuditLog
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "JSON invalide."}, status=400)
    name = (data.get("name") or "").strip()
    if not name:
        return JsonResponse({"status": "error", "message": "Nom obligatoire."}, status=400)
    if Offer.objects.filter(name=name).exists():
        return JsonResponse({"status": "error", "message": "Une offre avec ce nom existe déjà."}, status=400)
    bundle_price = Decimal(str(data.get("bundle_price") or "0"))
    page_ids = data.get("sales_page_ids") or []
    page_prices = data.get("page_prices") or {}  # {page_id: price}
    products_data = data.get("products") or []  # list of {product_id, quantity}

    with transaction.atomic():
        offer = Offer.objects.create(name=name, bundle_price=bundle_price)
        if page_ids:
            offer.sales_pages.set(SalesPage.objects.filter(id__in=page_ids))
        from .models import OfferPagePrice
        for pid, pr in (page_prices or {}).items():
            try:
                OfferPagePrice.objects.update_or_create(
                    offer=offer, sales_page_id=int(pid),
                    defaults={"price": Decimal(str(pr or "0"))},
                )
            except Exception:
                pass
        for p in products_data:
            try:
                OfferProduct.objects.create(
                    offer=offer,
                    product_id=int(p.get("product_id")),
                    quantity=max(int(p.get("quantity") or 1), 1),
                )
            except Exception:
                pass

    log_action(
        request.user, AuditLog.CREATE,
        description=f"Offre créée : '{offer.name}' à {offer.bundle_price} DT",
        request=request,
        target_model="Offer", target_id=offer.id,
    )
    return JsonResponse({"status": "ok", "offer_id": offer.id})


@csrf_exempt
@require_POST
@_admin_or_office
def api_offer_update(request, pk):
    from .models import Offer, SalesPage, Product, OfferProduct, log_action, AuditLog
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "JSON invalide."}, status=400)
    try:
        offer = Offer.objects.get(pk=pk)
    except Offer.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Offre introuvable."}, status=404)

    with transaction.atomic():
        if "name" in data:
            offer.name = data["name"].strip() or offer.name
        if "bundle_price" in data:
            offer.bundle_price = Decimal(str(data["bundle_price"]))
        if "is_active" in data:
            offer.is_active = bool(data["is_active"])
        offer.save()
        if "sales_page_ids" in data:
            offer.sales_pages.set(SalesPage.objects.filter(id__in=data["sales_page_ids"]))
        if "page_prices" in data:
            from .models import OfferPagePrice
            pp = data.get("page_prices") or {}
            # Replace all page prices with the submitted set.
            offer.page_prices.all().delete()
            for pid, pr in pp.items():
                try:
                    OfferPagePrice.objects.create(
                        offer=offer, sales_page_id=int(pid),
                        price=Decimal(str(pr or "0")),
                    )
                except Exception:
                    pass
        if "products" in data:
            offer.products.all().delete()
            for p in data["products"]:
                try:
                    OfferProduct.objects.create(
                        offer=offer,
                        product_id=int(p.get("product_id")),
                        quantity=max(int(p.get("quantity") or 1), 1),
                    )
                except Exception:
                    pass

    log_action(
        request.user, AuditLog.EDIT,
        description=f"Offre modifiée : '{offer.name}'",
        request=request,
        target_model="Offer", target_id=offer.id,
    )
    return JsonResponse({"status": "ok"})


@csrf_exempt
@require_POST
@_admin_only
def api_offer_delete(request, pk):
    from .models import Offer, log_action, AuditLog
    try:
        offer = Offer.objects.get(pk=pk)
    except Offer.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Offre introuvable."}, status=404)
    name = offer.name
    offer.delete()
    log_action(
        request.user, AuditLog.DELETE,
        description=f"Offre supprimée : '{name}'",
        request=request,
        target_model="Offer", target_id=pk,
    )
    return JsonResponse({"status": "ok"})


@login_required(login_url="/login/")
def order_view(request, pk):
    if not _orders_role_check(request):
        return redirect("home")
    from .models import Order
    order = get_object_or_404(
        Order.objects.select_related("customer", "region", "sales_page", "created_by")
                     .prefetch_related("lines__product", "lines__variant"),
        pk=pk,
    )
    return render(request, "inventory/order_view.html", {"order": order})


@csrf_exempt
@require_POST
@login_required(login_url="/login/")
def api_order_set_note(request, pk):
    """Save (or clear) the sticky note on an order. Used by the note icon in
    the orders list. Reuses the `status_note` field."""
    if not _orders_role_check(request):
        return JsonResponse({"status": "error", "message": "Accès refusé."}, status=403)
    from .models import Order, log_action, AuditLog
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "JSON invalide."}, status=400)
    order = get_object_or_404(Order, pk=pk)
    note = (data.get("note") or "").strip()[:300]
    order.status_note = note
    order.status_note_at = timezone.now() if note else None
    order.save(update_fields=["status_note", "status_note_at", "updated_at"])
    log_action(
        request.user, AuditLog.EDIT,
        description=f"Commande #{order.id} : note mise à jour" + (f" → {note}" if note else " (effacée)"),
        request=request, target_model="Order", target_id=order.id,
    )
    return JsonResponse({
        "status": "ok", "note": note,
        "note_at": order.status_note_at.strftime("%d/%m %H:%M") if order.status_note_at else "",
    })


@csrf_exempt
@require_POST
@login_required(login_url="/login/")
def api_order_change_status(request, pk):
    """Change the status of an order, enforcing valid transitions.

    Rules:
    - Once an order has a Navex barcode, the only allowed status change is to ANNULEE.
    - Setting status to CONFIRMEE auto-triggers a Navex push. If the push fails,
      status stays where it was and the error is returned.
    - Cancellation requires a `cancel_reason`. For 'rupture_stock', the system
      verifies the stock and refuses if the products are actually available.
    """
    if not _orders_role_check(request):
        return JsonResponse({"status": "error", "message": "Accès refusé."}, status=403)
    from .models import Order, log_action, AuditLog
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "JSON invalide."}, status=400)

    new_status = data.get("status", "")
    cancel_reason = (data.get("cancel_reason") or "").strip()
    valid = dict(Order.STATUS_CHOICES)
    if new_status not in valid:
        return JsonResponse({"status": "error", "message": "Statut invalide."}, status=400)

    order = get_object_or_404(Order, pk=pk)
    old_status = order.status
    old_label = order.get_status_display()

    # ---- Lock: if an order has a Navex barcode, only annulation is allowed ----
    if order.bordereau_barcode and new_status != Order.ANNULEE:
        return JsonResponse({
            "status": "error",
            "message": "Cette commande a déjà été envoyée à Navex. Seule une annulation est possible.",
            "code": "LOCKED_BY_NAVEX",
        }, status=400)

    # ---- Cancellation is only allowed from CONFIRMEE (or pre-Navex states). ----
    # Once an order is en route / at depot / returning / returned / delivered /
    # paid, it can no longer be cancelled — it's out of our hands at Navex.
    if new_status == Order.ANNULEE:
        cancellable_from = (
            Order.NON_CONFIRMEE, Order.CONFIRMEE, Order.RAPPELER,
            Order.INJOIGNABLE, Order.PAS_SERIEUX,
        )
        if old_status not in cancellable_from:
            return JsonResponse({
                "status": "error",
                "message": f"Impossible d'annuler une commande au statut « {old_label} ». "
                           f"Seules les commandes confirmées (ou non encore expédiées) peuvent être annulées.",
                "code": "NOT_CANCELLABLE",
            }, status=400)

    # ---- Confirmée → auto-push to Navex ----
    if new_status == Order.CONFIRMEE:
        if order.bordereau_barcode:
            return JsonResponse({
                "status": "error",
                "message": f"Déjà confirmée (bordereau {order.bordereau_barcode}).",
            }, status=400)
        # Delegate to the existing push function via internal call
        # Re-use the logic from api_push_order_to_navex
        return _push_order_to_navex_internal(request, order)

    # ---- Annulée → require a reason ----
    if new_status == Order.ANNULEE:
        valid_reasons = [Order.CANCEL_CLIENT, Order.CANCEL_CHANGEMENT, Order.CANCEL_RUPTURE]
        if cancel_reason not in valid_reasons:
            return JsonResponse({
                "status": "error",
                "message": "Raison d'annulation requise (client, changement ou rupture_stock).",
                "code": "REASON_REQUIRED",
            }, status=400)

        # Pre-cancel re-check: if a Navex bordereau exists, fetch the latest status
        # before doing anything. This catches cases where Navex moved the colis to
        # a non-cancellable state since our last sync (e.g. 'En magasin').
        if order.bordereau_barcode:
            _sync_navex_status_for_order(order)
            order.refresh_from_db()
            # Soft check: if we have a previously-known terminal/locked status,
            # refuse before even calling the cancel endpoint. We don't hardcode
            # which statuses lock — Navex's delete API will reject impossible
            # cancellations on its own. We just surface the latest status to the
            # user so they understand if it fails.

        # Rupture de stock: verify the products are actually out
        if cancel_reason == Order.CANCEL_RUPTURE:
            has_rupture, details = _check_order_stock_rupture(order)
            if not has_rupture:
                return JsonResponse({
                    "status": "error",
                    "message": "Les produits sont disponibles en stock — pas de rupture confirmée.",
                    "code": "NOT_RUPTURE",
                    "stock_details": details,
                }, status=400)

        # If a Navex bordereau exists, we MUST cancel it on Navex first.
        navex_was_cancelled = False
        navex_response = None
        if order.bordereau_barcode:
            ok, navex_response = _navex_cancel_colis(order.bordereau_barcode)
            if not ok:
                # Cancellation on Navex failed → don't change anything in our DB
                err_msg = ""
                if isinstance(navex_response, dict):
                    err_msg = navex_response.get("status_message") or \
                              navex_response.get("message") or \
                              navex_response.get("_error") or \
                              navex_response.get("_raw") or ""
                log_action(
                    request.user, AuditLog.OTHER,
                    description=f"Annulation refusée par Navex pour #{order.id} (bordereau {order.bordereau_barcode}): {str(navex_response)[:200]}",
                    request=request, target_model="Order", target_id=order.id,
                    extra=str(navex_response)[:5000],
                )
                return JsonResponse({
                    "status": "error",
                    "message": f"Navex a refusé la suppression : {err_msg or 'erreur inconnue'}. La commande n'a pas été annulée.",
                    "code": "NAVEX_CANCEL_FAILED",
                    "navex_response": navex_response,
                }, status=400)
            navex_was_cancelled = True
            log_action(
                request.user, AuditLog.OTHER,
                description=f"Bordereau {order.bordereau_barcode} supprimé sur Navex (commande #{order.id})",
                request=request, target_model="Order", target_id=order.id,
                extra=str(navex_response)[:5000],
            )

        # Branch: "Annulé pour changement" REVERTS the order to non_confirmee + editable
        if cancel_reason == Order.CANCEL_CHANGEMENT:
            old_bordereau = order.bordereau_barcode
            order.status = Order.NON_CONFIRMEE
            order.bordereau_barcode = ""
            order.navex_label_url = ""
            order.pushed_to_navex_at = None
            # Don't set cancel_reason or cancelled_at — order is "live" again
            order.save(update_fields=[
                "status", "bordereau_barcode", "navex_label_url",
                "pushed_to_navex_at", "updated_at",
            ])
            log_action(
                request.user, AuditLog.STATUS_CHANGE,
                description=f"Commande #{order.id} ré-ouverte pour modification (ancien bordereau {old_bordereau} supprimé sur Navex)",
                request=request, target_model="Order", target_id=order.id,
            )
            return JsonResponse({
                "status": "ok",
                "new_status": Order.NON_CONFIRMEE,
                "label": "Non confirmée",
                "reopened": True,
                "navex_was_cancelled": navex_was_cancelled,
                "redirect": f"/sales-orders/{order.id}/",
            })

        # Standard cancellation (client / rupture_stock): mark annulee + reason
        order.status = Order.ANNULEE
        order.cancel_reason = cancel_reason
        order.cancelled_at = timezone.now()
        order.save(update_fields=["status", "cancel_reason", "cancelled_at", "updated_at"])

        # If the order came from Shopify, also cancel it on their side so it
        # doesn't stay "open" in their admin.
        shopify_cancelled = False
        shopify_id = _extract_shopify_order_id_from_notes(order.notes)
        if shopify_id:
            ok_sh, sh_resp = _shopify_cancel_order(shopify_id)
            if ok_sh:
                shopify_cancelled = True
                log_action(
                    request.user, AuditLog.OTHER,
                    description=f"Shopify : commande {shopify_id} annulée (Order #{order.id})",
                    request=request, target_model="Order", target_id=order.id,
                    extra=str(sh_resp)[:5000],
                )
            else:
                # Don't block — just log. The team can fix on Shopify manually.
                log_action(
                    request.user, AuditLog.OTHER,
                    description=f"Shopify : ÉCHEC d'annulation pour {shopify_id} (Order #{order.id}) — à fixer manuellement",
                    request=request, target_model="Order", target_id=order.id,
                    extra=str(sh_resp)[:5000],
                )

        # If the order came from Converty, push the cancellation (rejected).
        if getattr(order, "converty_order_id", ""):
            try:
                from .converty import push_status_to_converty
                push_status_to_converty(order, Order.ANNULEE)
            except Exception:
                pass

        log_action(
            request.user, AuditLog.STATUS_CHANGE,
            description=(
                f"Commande #{order.id} annulée : "
                f"{dict(Order.CANCEL_REASON_CHOICES).get(cancel_reason, cancel_reason)}"
                + (" (bordereau Navex également supprimé)" if navex_was_cancelled else "")
                + (" (Shopify également annulé)" if shopify_cancelled else "")
            ),
            request=request,
            target_model="Order", target_id=order.id,
        )
        return JsonResponse({
            "status": "ok", "new_status": Order.ANNULEE,
            "label": valid[Order.ANNULEE],
            "cancel_reason": cancel_reason,
            "navex_was_cancelled": navex_was_cancelled,
            "shopify_was_cancelled": shopify_cancelled,
        })

    # ---- Other simple transitions (injoignable, pas_serieux, rappeler_plus_tard) ----
    # Valid transitions table
    allowed_transitions = {
        Order.NON_CONFIRMEE: [Order.INJOIGNABLE, Order.PAS_SERIEUX, Order.RAPPELER, Order.ANNULEE],
        Order.RAPPELER:      [Order.NON_CONFIRMEE, Order.INJOIGNABLE, Order.PAS_SERIEUX, Order.ANNULEE],
        Order.INJOIGNABLE:   [Order.NON_CONFIRMEE, Order.RAPPELER, Order.PAS_SERIEUX, Order.ANNULEE],
        Order.PAS_SERIEUX:   [Order.ANNULEE],
        Order.CONFIRMEE:     [Order.ANNULEE],   # only via cancellation flow above
        Order.ANNULEE:       [],                # frozen (admin only)
    }
    if new_status not in allowed_transitions.get(old_status, []):
        return JsonResponse({
            "status": "error",
            "message": f"Transition non permise : {old_label} → {valid[new_status]}.",
            "code": "INVALID_TRANSITION",
        }, status=400)

    # These statuses require a note explaining the reason.
    note_required = (Order.INJOIGNABLE, Order.RAPPELER, Order.PAS_SERIEUX)
    status_note = (data.get("status_note") or "").strip()
    if new_status in note_required and not status_note:
        return JsonResponse({
            "status": "error",
            "message": "Une note est obligatoire pour ce statut.",
            "code": "NOTE_REQUIRED",
        }, status=400)

    order.status = new_status
    update_fields = ["status", "updated_at"]
    if status_note:
        order.status_note = status_note[:300]
        order.status_note_at = timezone.now()
        update_fields.append("status_note")
        update_fields.append("status_note_at")
    order.save(update_fields=update_fields)
    _maybe_send_status_sms(order)
    log_action(
        request.user, AuditLog.STATUS_CHANGE,
        description=f"Commande #{order.id} : {old_label} → {valid[new_status]}"
                    + (f" — note: {status_note}" if status_note else ""),
        request=request,
        target_model="Order", target_id=order.id,
    )
    return JsonResponse({"status": "ok", "new_status": new_status, "label": valid[new_status]})


def _navex_clean_text(s):
    """Sanitize a text field before sending to Navex. Navex's PHP endpoint can
    return HTTP 500 when fed 'fancy' Unicode (stylized math/fullwidth letters
    people use on social media) or emoji. We NFKC-normalize (folds fancy fonts
    back to plain letters) and drop symbols/emoji, keeping letters (incl.
    Arabic), numbers, punctuation and spaces."""
    import unicodedata
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    out = []
    for ch in s:
        cat = unicodedata.category(ch)
        # keep Letters, Marks, Numbers, Punctuation, Spaces; drop Symbols (emoji),
        # control/surrogate chars, etc.
        if cat[0] in ("L", "M", "N", "P", "Z") or ch in " -'":
            out.append(ch)
    cleaned = "".join(out).strip()
    # collapse runs of whitespace
    cleaned = " ".join(cleaned.split())
    return cleaned


def _push_order_to_navex_internal(request, order):
    """Internal: push an order to Navex and return JsonResponse with the result.
    Same logic as api_push_order_to_navex but takes an already-loaded order."""
    import urllib.request, urllib.parse
    from .models import Order, log_action, AuditLog

    token = os.environ.get("NAVEX_API_TOKEN", "")
    if not token:
        return JsonResponse({"status": "error", "message": "NAVEX_API_TOKEN non configuré côté serveur."}, status=500)

    if order.bordereau_barcode:
        return JsonResponse({
            "status": "error",
            "message": f"Déjà envoyé à Navex (bordereau {order.bordereau_barcode}).",
        }, status=400)
    if not order.customer.phone:
        return JsonResponse({"status": "error", "message": "Téléphone manquant."}, status=400)
    if not order.region:
        return JsonResponse({"status": "error", "message": "Gouvernorat manquant."}, status=400)
    if not (order.address or "").strip():
        return JsonResponse({"status": "error", "message": "Adresse manquante."}, status=400)

    # Require at least one article/offer (not for exchanges, which can be
    # return-only). An order with no offers and no lines must not be confirmed.
    if not order.exchange_of_id:
        has_offers = order.order_offers.exists()
        has_lines = order.lines.exists()
        if not has_offers and not has_lines:
            return JsonResponse({
                "status": "error",
                "message": "Aucun article sélectionné. Ajoutez au moins une offre avant de confirmer.",
            }, status=400)

    # Refuse to confirm if any article is missing its SIZE. Staff were
    # confirming orders without noticing a blank size, which then ship/print
    # wrong. A size is required on every line (except exchange placeholders).
    missing_size = []
    for line in order.lines.all():
        if not (line.size or "").strip():
            pname = line.product.name if line.product else "Article"
            missing_size.append(pname)
    if missing_size:
        uniq = list(dict.fromkeys(missing_size))  # de-dup, keep order
        return JsonResponse({
            "status": "error",
            "message": "Taille manquante pour : " + ", ".join(uniq)
                       + ". Veuillez choisir la taille avant de confirmer.",
        }, status=400)

    # Defensive: recompute total right before push, in case it was never updated.
    # Without this, a draft created via autosave but never recalculated would push prix=0.
    order.recalc_total()
    order.refresh_from_db()
    # For NORMAL orders, refuse if total=0 (probably no articles selected).
    # For EXCHANGE orders, total can legitimately be 0 (notre faute → free reshipping)
    # so we don't apply this check.
    if not order.exchange_of_id:
        if not order.total or order.total <= 0:
            return JsonResponse({
                "status": "error",
                "message": "Prix de la commande est 0. Vérifiez que des articles sont bien sélectionnés.",
            }, status=400)

    # If this is an exchange, require that return items have been selected
    if order.exchange_of_id and not order.return_items.exists():
        return JsonResponse({
            "status": "error",
            "message": "Cette commande est un échange mais aucun article retourné n'a été sélectionné. Cliquez sur le bouton '🔄 Retour' pour choisir les articles.",
        }, status=400)

    designation = _build_designation(order)
    nb_article = _count_articles(order)

    # If this is an exchange order, build the exchange-specific params.
    # - echange     : ID of the original delivered order (for our tracking)
    # - article     : (empty, Navex doesn't need a description of returned items)
    # - nb_echange  : count of items being returned
    # - ouvrir      : "Oui" — the client can open and verify the new colis
    # PRICE for exchange: only the delivery fee (default 7 DT) minus discount.
    # The articles' value is NOT charged again — client already paid for those
    # on the original delivered order.
    exchange_str = ""
    article_str = ""
    nb_echange_str = ""
    ouvrir_str = "Oui"  # clients are allowed to open & verify the colis before paying
    exchange_price = None  # if set, overrides the normal order.total
    if order.exchange_of_id:
        exchange_str = str(order.exchange_of_id)
        nb_returns = order.return_items.count()
        nb_echange_str = str(nb_returns) if nb_returns else ""
        ouvrir_str = "Oui"
        # For exchanges: only delivery_fee - discount.
        # If our fault, the team sets discount=delivery_fee → 0 DT.
        # If client takes a more expensive product, team raises delivery_fee.
        from decimal import Decimal
        exchange_price = max(
            Decimal("0"),
            (order.delivery_fee or Decimal("0")) - (order.discount or Decimal("0"))
        )

    prix_str = f"{exchange_price:.0f}" if exchange_price is not None else (f"{order.total:.0f}" if order.total else "0")

    # Standard message appended to every Navex order's "msg" field.
    # Informs the client about exchange policy: 2 days max, no refunds, only exchanges.
    POLICY_MSG = (
        "Les échanges sont acceptés uniquement dans un délai maximum de 2 jours "
        "après réception du colis (échange uniquement, pas de remboursement). "
        "Pour toute demande d'échange, veuillez nous contacter immédiatement au 26200219."
    )
    # order.notes mixes INTERNAL metadata (shopify id, ad source, campaign name
    # with emoji, "créée depuis Messenger") with any real delivery note. Navex
    # must NOT receive the internal lines — they're for us only, and emoji in
    # the campaign name makes Navex's PHP endpoint return HTTP 500. Keep only
    # genuine client notes, then sanitize.
    _internal_prefixes = (
        "shopify_order_id", "shopify_order_number", "ad source", "campagne",
        "[commande créée", "source_campaign", "ctwa", "ref:",
    )
    _kept_lines = []
    for _line in (order.notes or "").replace("|", "\n").split("\n"):
        _l = _line.strip()
        if not _l:
            continue
        if any(_l.lower().startswith(p) for p in _internal_prefixes):
            continue
        _kept_lines.append(_l)
    user_notes = _navex_clean_text(" ".join(_kept_lines)).strip()
    if user_notes:
        msg_str = f"{user_notes} | {POLICY_MSG}"
    else:
        msg_str = POLICY_MSG
    msg_str = _navex_clean_text(msg_str)

    payload = {
        "prix":           prix_str,
        "nom":            _navex_clean_text(order.display_name or order.customer.phone),
        "gouvernerat":    order.region.name,
        "ville":          _navex_clean_text(order.ville or ""),
        "adresse":        _navex_clean_text((order.address or order.localite or "").strip() or order.ville or ""),
        "tel":            order.customer.phone,
        "tel2":           order.customer.phone2 or "",
        "designation":    _navex_clean_text(designation)[:500],
        "nb_article":     str(nb_article),
        "msg":            msg_str[:500],
        "echange":        exchange_str,
        "article":        article_str,
        "nb_echange":     nb_echange_str,
        "ouvrir":         ouvrir_str,
        "sender_name":    "", "sender_location": "",
    }
    url = f"https://app.navex.tn/api/rashop-{token}/v1/post.php"
    try:
        body = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                navex_data = json.loads(raw)
            except json.JSONDecodeError:
                navex_data = {"_raw": raw}
    except Exception as e:
        log_action(
            request.user, AuditLog.NAVEX_PUSH,
            description=f"Push #{order.id} ÉCHEC réseau : {str(e)[:200]}",
            request=request, target_model="Order", target_id=order.id,
        )
        return JsonResponse({"status": "error", "message": f"Erreur réseau Navex : {e}"}, status=502)

    if not _navex_response_is_success(navex_data):
        log_action(
            request.user, AuditLog.NAVEX_PUSH,
            description=f"Push #{order.id} REFUSÉ par Navex : {str(navex_data)[:300]}",
            request=request, target_model="Order", target_id=order.id,
            extra=str(navex_data)[:5000],
        )
        return JsonResponse({
            "status": "error",
            "message": navex_data.get("status_message") or navex_data.get("message") or "Navex a refusé la commande.",
            "navex_response": navex_data,
        }, status=400)

    bordereau = _extract_bordereau_from_navex_response(navex_data)
    label_url = navex_data.get("lien") or ""
    order.pushed_to_navex_at = timezone.now()
    if bordereau:
        order.bordereau_barcode = bordereau
    if label_url:
        order.navex_label_url = label_url[:500]
    order.status = Order.CONFIRMEE
    order.save(update_fields=["bordereau_barcode", "navex_label_url", "pushed_to_navex_at", "status", "updated_at"])

    # If the order came from Converty, push the confirmation.
    if getattr(order, "converty_order_id", ""):
        try:
            from .converty import push_status_to_converty
            push_status_to_converty(order, Order.CONFIRMEE)
        except Exception:
            pass

    is_exchange_msg = f" [ÉCHANGE de #{order.exchange_of_id}, {order.return_items.count()} article(s) retour]" if order.exchange_of_id else ""
    log_action(
        request.user, AuditLog.NAVEX_PUSH,
        description=f"Commande #{order.id} envoyée à Navex{is_exchange_msg}" + (f" — bordereau {bordereau}" if bordereau else " (bordereau manquant)"),
        request=request, target_model="Order", target_id=order.id,
        extra=str(navex_data)[:5000],
    )
    return JsonResponse({
        "status": "ok",
        "bordereau": bordereau,
        "label_url": label_url,
        "new_status": Order.CONFIRMEE,
        "label": "Confirmée",
        "redirect": f"/sales-orders/{order.id}/",
    })


# ---------------------------------------------------------------------------
# PHASE 5 — PUSH ORDER TO NAVEX
# Sends a confirmed order to Navex for shipping. On success Navex returns a
# bordereau code which we save back to order.bordereau_barcode.
# ---------------------------------------------------------------------------

def _build_designation(order):
    """Build the Navex 'designation' field in the format the customer expects:

        "94009 Converti | polo ralph kaja summer bleu (M), polo ralph kaja summer beige (M)"

    Breakdown:
        - "94009"     → our order.id
        - "Converti"  → name of the Facebook SalesPage attached to the order
        - " | "       → separator
        - products    → "{product_name} {color} ({size})", repeated once per unit
                        (e.g. quantity=2 → product listed twice, no "2x" prefix)
    """
    units = []  # one string per physical unit being shipped

    def render_unit(line):
        seg = line.product.name
        if line.variant:
            seg += f" {line.variant.color_label or line.variant.color_name}"
        if line.size:
            seg += f" ({line.size})"
        return seg

    # Lines that are part of an offer (multiply quantity by the offer's quantity)
    for oo in order.order_offers.all():
        offer_mult = max(oo.quantity, 1)
        for line in oo.lines.all():
            effective_qty = line.quantity * offer_mult
            unit_str = render_unit(line)
            for _ in range(effective_qty):
                units.append(unit_str)

    # Standalone lines (not part of any offer)
    for line in order.lines.filter(order_offer__isnull=True):
        unit_str = render_unit(line)
        for _ in range(max(line.quantity, 1)):
            units.append(unit_str)

    products_str = ", ".join(units) if units else "Commande"
    page_name = order.sales_page.name if order.sales_page else ""
    prefix = f"{order.id} {page_name}".strip()
    return f"{prefix} | {products_str}" if prefix else products_str


def _check_order_stock_rupture(order):
    """For each line in the order, check if the (variant, size) is actually
    in stock. Returns (is_rupture, details_list) where:
        is_rupture = True if at least one line has 0 in_stock+returned units
        details_list = list of {product, variant, size, in_stock_count}
    Used to verify a 'rupture de stock' cancellation reason.
    """
    details = []
    has_rupture = False
    for line in order.lines.all():
        variant = line.variant
        size = line.size
        if not variant or not size:
            continue
        # Count units in_stock or returned (sellable) at this variant+size
        count = variant.units.filter(
            status__in=[ProductUnit.IN_STOCK, ProductUnit.RETURNED],
            size=size,
        ).count()
        details.append({
            "product": line.product.name,
            "variant": variant.color_label or variant.color_name,
            "size": size,
            "needed": line.quantity,
            "in_stock": count,
            "is_short": count < line.quantity,
        })
        if count < line.quantity:
            has_rupture = True
    return has_rupture, details
    """Plain-text description of articles for Navex's 'designation' field."""
    parts = []
    for oo in order.order_offers.all():
        # Show the offer + its products (with chosen variant/size if any)
        offer_parts = []
        for line in oo.lines.all():
            seg = line.product.name
            if line.variant:
                seg += f" {line.variant.color_label or line.variant.color_name}"
            if line.size:
                seg += f" {line.size}"
            if line.quantity > 1:
                seg = f"{line.quantity}× {seg}"
            offer_parts.append(seg)
        offer_label = oo.offer_name or "Offre"
        if oo.quantity > 1:
            offer_label = f"{oo.quantity}× {offer_label}"
        if offer_parts:
            parts.append(f"{offer_label} ({', '.join(offer_parts)})")
        else:
            parts.append(offer_label)
    # Standalone lines (no offer parent)
    for line in order.lines.filter(order_offer__isnull=True):
        seg = line.product.name
        if line.variant:
            seg += f" {line.variant.color_label or line.variant.color_name}"
        if line.size:
            seg += f" {line.size}"
        if line.quantity > 1:
            seg = f"{line.quantity}× {seg}"
        parts.append(seg)
    return " | ".join(parts) if parts else "Commande"


def _count_articles(order):
    """Total number of physical articles for nb_article."""
    n = 0
    for oo in order.order_offers.all():
        for line in oo.lines.all():
            n += line.quantity
        if not oo.lines.exists():
            n += oo.quantity  # offer with no detailed lines counts as itself
    for line in order.lines.filter(order_offer__isnull=True):
        n += line.quantity
    return max(n, 1)


def _extract_bordereau_from_navex_response(resp_data):
    """Find the bordereau code in Navex's response.

    Real Navex success response (verified 2026-05-08):
        {'status': 1, 'lien': '...', 'status_message': '918762425951'}

    The bordereau is the digit string in status_message.
    Fallback: look for known field names in case Navex changes the format.
    """
    if not isinstance(resp_data, dict):
        return ""
    # Real-world success: status_message is the bordereau (a digit string)
    sm = resp_data.get("status_message", "")
    if isinstance(sm, (str, int)):
        s = str(sm).strip()
        if s.isdigit() and len(s) >= 6:  # looks like a bordereau number
            return s
    # Fallback: try other plausible keys
    for key in ("code", "barcode", "bordereau", "bordereau_barcode", "tracking", "id"):
        v = resp_data.get(key)
        if v and isinstance(v, (str, int)):
            return str(v).strip()
    colis = resp_data.get("colis")
    if isinstance(colis, dict):
        for key in ("code", "barcode", "bordereau", "bordereau_barcode", "tracking", "id"):
            v = colis.get(key)
            if v and isinstance(v, (str, int)):
                return str(v).strip()
    return ""


def _navex_response_is_success(resp_data):
    """Detect a successful Navex create response.

    Real Navex success: status == 1 (integer) and status_message contains a digit string.
    Failure: status_message contains 'ERREUR' or status is something else.
    """
    if not isinstance(resp_data, dict):
        return False
    status = resp_data.get("status")
    if status == 1 or status == "1" or status == "ok" or status == "success":
        return True
    # Fallback: success if we can extract a bordereau-looking number anywhere
    bc = _extract_bordereau_from_navex_response(resp_data)
    if bc and bc.isdigit() and len(bc) >= 6:
        return True
    return False


def _navex_fetch_one(bordereau):
    """Fetch the current Navex status for a single bordereau.
    Returns (ok: bool, parsed: dict, raw_response: dict|str).
    The parsed dict has keys: etat, motif, pre_etat, livreur, livreur_tel.
    Uses the bulk endpoint with codes= for consistency.
    """
    ok, items, raw = _navex_fetch_many([bordereau])
    if not ok or not items:
        return False, {}, raw
    return True, items.get(bordereau, {}), raw


def _navex_fetch_many(bordereaux):
    """Fetch Navex status for a list of bordereaux in ONE API call.
    Returns (ok: bool, items_by_code: dict, raw_response: dict|str).

    Uses `codes=` (plural, comma-separated) + `include_echange=1` to also
    get the exchange return barcode for orders that are exchanges.
    """
    import urllib.request, urllib.parse
    token = os.environ.get("NAVEX_API_TOKEN", "")
    if not token or not bordereaux:
        return False, {}, {"_error": "missing token or bordereaux"}

    url = f"https://app.navex.tn/api/rashop-etat-{token}/v1/post.php"
    bordereaux = [b for b in bordereaux if b]
    if not bordereaux:
        return False, {}, {"_error": "no valid bordereau"}

    codes_string = ", ".join(bordereaux)
    payload_dict = {
        "codes": codes_string,
        "include_echange": "1",  # Navex returns code_echange + date_echange for exchanges
    }

    try:
        body = urllib.parse.urlencode(payload_dict).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                return False, {}, {"_raw": raw}
    except Exception as e:
        return False, {}, {"_error": f"network: {e}"}

    items = {}
    if isinstance(data, dict):
        results = data.get("results") or []
        if isinstance(results, list):
            for entry in results:
                if not isinstance(entry, dict):
                    continue
                code = str(entry.get("code", "")).strip()
                if not code:
                    continue
                # Navex returns "code_echange" + "date_echange" for exchange orders
                # when include_echange=1 is in the payload.
                code_echange = str(entry.get("code_echange") or "").strip()
                date_echange = str(entry.get("date_echange") or "").strip()
                items[code] = {
                    "etat":           str(entry.get("etat") or "").strip(),
                    "motif":          str(entry.get("motif") or "").strip(),
                    "pre_etat":       str(entry.get("pre_etat") or "").strip(),
                    "livreur":        str(entry.get("livreur") or "").strip(),
                    "livreur_tel":    str(entry.get("livreur_tel") or "").strip(),
                    "found":          bool(entry.get("status") == 1),
                    "code_echange":   code_echange,
                    "date_echange":   date_echange,
                }
    return True, items, data


def _sync_navex_status_for_order(order, force=False):
    """Refresh navex status fields for a single Order. Returns True if updated."""
    if not order.bordereau_barcode:
        return False
    ok, parsed, raw = _navex_fetch_one(order.bordereau_barcode)
    if not ok or not parsed:
        return False
    order.navex_last_status   = (parsed.get("etat") or "")[:80]
    order.navex_motif         = (parsed.get("motif") or "")[:200]
    order.navex_pre_etat      = (parsed.get("pre_etat") or "")[:80]
    order.navex_livreur       = (parsed.get("livreur") or "")[:120]
    order.navex_livreur_tel   = (parsed.get("livreur_tel") or "")[:30]
    order.navex_last_status_raw = str(raw)[:5000]
    order.navex_last_synced_at = timezone.now()
    update_fields = [
        "navex_last_status", "navex_motif", "navex_pre_etat",
        "navex_livreur", "navex_livreur_tel",
        "navex_last_status_raw", "navex_last_synced_at", "updated_at",
    ]
    # If Navex returned a code_echange for this order (it's an exchange),
    # store it as the return barcode.
    code_echange = parsed.get("code_echange") or ""
    if code_echange and code_echange != order.navex_return_barcode:
        order.navex_return_barcode = code_echange[:80]
        update_fields.append("navex_return_barcode")
    order.save(update_fields=update_fields)
    return True


def _sync_navex_for_v2_orders(only_pending=True):
    """Bulk-refresh Navex status for v2 orders that have a bordereau.

    Uses Navex's bulk endpoint: ALL orders synced in ONE API call.
    only_pending=True (default): skip orders already in Annulée status.
    Returns (n_attempted, n_updated).
    """
    from .models import Order, log_action, AuditLog
    from django.db.models import Q
    from datetime import timedelta
    qs = Order.objects.exclude(bordereau_barcode="")
    if only_pending:
        # Skip annulees and supprime_navex. For LIVREE: keep polling any
        # delivered order that doesn't yet have a navex_return_barcode, so a
        # code_echange generated later by Navex (e.g. client kept part of the
        # order at the door) still gets fetched. Once the return barcode is
        # stored, the order drops out of the sync.
        #
        # Stop condition: a delivered order whose linked v1 ShippingOrder has
        # been paid for more than 24h is considered settled — drop it from the
        # sync even if no return barcode ever came. This keeps the polling set
        # from growing forever with normal deliveries.
        final_states = (Order.ANNULEE, Order.SUPPRIME_NAVEX, Order.RETURNED, Order.PAYEE)
        qs = qs.exclude(status__in=final_states)
        paid_cutoff = timezone.now() - timedelta(hours=24)
        # A LIVREE order is "settled" once any linked paid ShippingOrder was
        # paid more than 24h ago. Such orders are excluded.
        settled_livree = (
            Q(status=Order.LIVREE)
            & Q(shipping_orders__status=ShippingOrder.PAID)
            & Q(shipping_orders__paid_at__lt=paid_cutoff)
        )
        # Keep: anything not LIVREE, OR a LIVREE still missing its return
        # barcode AND not yet settled (paid >24h ago).
        qs = qs.filter(
            ~Q(status=Order.LIVREE)
            | (Q(status=Order.LIVREE) & Q(navex_return_barcode=""))
        ).exclude(settled_livree).distinct()
    orders = list(qs)
    n_attempted = len(orders)
    if n_attempted == 0:
        return 0, 0

    # Bulk fetch
    bordereaux = [o.bordereau_barcode for o in orders]
    ok, items, raw = _navex_fetch_many(bordereaux)
    if not ok:
        return n_attempted, 0

    n_updated = 0
    now = timezone.now()
    raw_str = str(raw)[:5000]
    for o in orders:
        parsed = items.get(o.bordereau_barcode)
        if not parsed:
            continue
        new_navex_status = (parsed.get("etat") or "")[:80]
        o.navex_last_status   = new_navex_status
        o.navex_motif         = (parsed.get("motif") or "")[:200]
        o.navex_pre_etat      = (parsed.get("pre_etat") or "")[:80]
        o.navex_livreur       = (parsed.get("livreur") or "")[:120]
        o.navex_livreur_tel   = (parsed.get("livreur_tel") or "")[:30]
        o.navex_last_status_raw = raw_str
        o.navex_last_synced_at = now
        update_fields = [
            "navex_last_status", "navex_motif", "navex_pre_etat",
            "navex_livreur", "navex_livreur_tel",
            "navex_last_status_raw", "navex_last_synced_at", "updated_at",
        ]
        # If Navex returned a code_echange for this order (it's an exchange),
        # store it as the return barcode. Only update if it's new/different.
        code_echange = (parsed.get("code_echange") or "").strip()
        if code_echange and code_echange != o.navex_return_barcode:
            o.navex_return_barcode = code_echange[:80]
            update_fields.append("navex_return_barcode")
            log_action(
                None, AuditLog.EDIT,
                description=f"Auto: code d'échange Navex récupéré pour #{o.id} → {code_echange}",
                target_model="Order", target_id=o.id,
            )
        # Detect "Supprime" Navex status — means the colis was deleted on their side
        # after our push. We auto-transition the local order to SUPPRIME_NAVEX status
        # so it surfaces clearly (red badge + dedicated filter).
        # Only do this when our local status is still confirmee (i.e. we hadn't
        # already cancelled/annulled it ourselves).
        if (new_navex_status.strip().lower() in ("supprime", "supprimé", "deleted")
                and o.status == Order.CONFIRMEE):
            o.status = Order.SUPPRIME_NAVEX
            update_fields.append("status")
            log_action(
                None, AuditLog.STATUS_CHANGE,
                description=f"Auto: commande #{o.id} passée en 'Supprimé Navex' (sync a détecté 'Supprime' chez Navex, bordereau {o.bordereau_barcode})",
                target_model="Order", target_id=o.id,
            )
        # Detect "Livré" Navex status → auto-transition to our LIVREE status.
        # Variants: "Livré", "Livré Payé", "Livrer", "Livrer Paye", "Livree".
        # Allowed from any forward in-transit state (Confirmée / En cours /
        # Au magasin) — the colis can be delivered after passing through those.
        navex_lower = new_navex_status.strip().lower()
        if (navex_lower in (
                "livre", "livré", "livree", "livrée",
                "livrer", "livrer paye", "livré payé", "livre paye", "livre payé",
            )
            and o.status in (Order.CONFIRMEE, Order.EN_COURS, Order.AU_MAGASIN)):
            old_label = dict(Order.STATUS_CHOICES).get(o.status, o.status)
            o.status = Order.LIVREE
            if o.delivered_at is None:
                o.delivered_at = now
                if "delivered_at" not in update_fields:
                    update_fields.append("delivered_at")
            if "status" not in update_fields:
                update_fields.append("status")
            log_action(
                None, AuditLog.STATUS_CHANGE,
                description=f"Auto: commande #{o.id} {old_label} → 'Livrée' (Navex etat='{new_navex_status}', bordereau {o.bordereau_barcode})",
                target_model="Order", target_id=o.id,
            )
            # If the order came from Converty, push 'delivered'.
            if getattr(o, "converty_order_id", ""):
                try:
                    from .converty import push_status_to_converty
                    push_status_to_converty(o, Order.LIVREE)
                except Exception:
                    pass

        # If Navex reports the colis as "Livré Payé" (delivered AND paid), record
        # the moment we first detected it on the linked v1 ShippingOrder(s). The
        # office's later pay-confirmation uses this as paid_at (≈ real Navex date)
        # instead of the click time. Set once; never overwrite.
        if navex_lower in ("livrer paye", "livré payé", "livre paye", "livre payé", "livree paye", "livrée payée"):
            for so in o.shipping_orders.all():
                if so.navex_paid_detected_at is None:
                    so.navex_paid_detected_at = now
                    so.save(update_fields=["navex_paid_detected_at"])

        # Detect "Au magasin" / "En cours" → set the in-transit status. Allowed
        # from Confirmée or between the two in-transit states themselves
        # (the colis can bounce en_cours <-> au_magasin). Don't disturb final
        # or return states.
        if o.status in (Order.CONFIRMEE, Order.EN_COURS, Order.AU_MAGASIN):
            new_v2_status = None
            if navex_lower in ("au magasin", "au-magasin", "au magasin navex"):
                new_v2_status = Order.AU_MAGASIN
            elif navex_lower in ("en cours", "en-cours", "en cours de livraison"):
                new_v2_status = Order.EN_COURS
            if new_v2_status and new_v2_status != o.status:
                old_label = dict(Order.STATUS_CHOICES).get(o.status, o.status)
                o.status = new_v2_status
                if "status" not in update_fields:
                    update_fields.append("status")
                log_action(
                    None, AuditLog.STATUS_CHANGE,
                    description=f"Auto: commande #{o.id} {old_label} → '{dict(Order.STATUS_CHOICES)[new_v2_status]}' "
                                f"(Navex etat='{new_navex_status}', bordereau {o.bordereau_barcode})",
                    target_model="Order", target_id=o.id,
                )

        # Detect "Retour Expéditeur" / "Rtn client/agence" → move the order into
        # RETURNING ("En retour"). Can come from Confirmée or an in-transit
        # status (en_cours / au_magasin). NOTE: the final RETURNED status is set
        # by physical scan in v1, not from Navex sync.
        if o.status in (Order.CONFIRMEE, Order.EN_COURS, Order.AU_MAGASIN):
            if navex_lower in ("retour expediteur", "retour expéditeur",
                               "retour vers expediteur", "retour vers expéditeur",
                               "rtn client/agence", "rtn client", "rtn agence"):
                old_label = dict(Order.STATUS_CHOICES).get(o.status, o.status)
                o.status = Order.RETURNING
                if "status" not in update_fields:
                    update_fields.append("status")
                log_action(
                    None, AuditLog.STATUS_CHANGE,
                    description=f"Auto: commande #{o.id} {old_label} → 'En retour' "
                                f"(Navex etat='{new_navex_status}', bordereau {o.bordereau_barcode})",
                    target_model="Order", target_id=o.id,
                )

        # Detect "Rtn client/agence" → mark the order's ProductUnits as EARLY_RETURN
        # (the customer refused; the parcel is on its way back to us).
        # Only flip units that are currently SHIPPED — don't touch PAID/RETURNED.
        if navex_lower in ("rtn client/agence", "rtn client", "rtn agence", "retour anticipe", "retour anticipé"):
            _flip_order_units_status(o, ProductUnit.SHIPPED, ProductUnit.EARLY_RETURN, "early_return")

        # Detect "Retour recu" → unit at Navex hub, waiting for our physical pickup.
        # Flip EARLY_RETURN and SHIPPED units → AT_DEPOT.
        if navex_lower in ("retour recu", "retour reçu", "retourne", "retourné", "retour confirme", "retour confirmé"):
            _flip_order_units_status(o, ProductUnit.EARLY_RETURN, ProductUnit.AT_DEPOT, "at_depot")
            # Also catch cases where the SHIPPED→EARLY_RETURN step was skipped
            # (Navex jumped straight to "Retour recu") — flip those too.
            _flip_order_units_status(o, ProductUnit.SHIPPED, ProductUnit.AT_DEPOT, "at_depot")

        # NOTE: confirmed return → RETURNED happens via physical scan in the warehouse,
        # not from Navex sync. Otherwise we'd report "back in stock" before it's actually back.

        o.save(update_fields=update_fields)
        n_updated += 1
    return n_attempted, n_updated


def _flip_order_units_status(order, from_status, to_status, movement_type):
    """For all ProductUnits linked to the v2 Order (via ShippingOrder.order),
    if currently in `from_status`, flip to `to_status` and record a StockMovement.

    Used by the Navex sync to auto-mark units as 'early_return' or 'returned'
    based on the order's Navex status, so the warehouse team sees the correct
    physical state without scanning each unit one by one.
    """
    from .models import log_action, AuditLog, StockMovement
    n_flipped = 0
    for so in order.shipping_orders.all():
        for item in so.items.select_related("unit"):
            unit = item.unit
            if unit and unit.status == from_status:
                unit.status = to_status
                unit.save(update_fields=["status", "updated_at"])
                StockMovement.objects.create(
                    unit=unit,
                    movement_type=movement_type,
                    reference=f"Auto sync Navex — commande v2 #{order.id}",
                )
                n_flipped += 1
    if n_flipped:
        log_action(
            None, AuditLog.STATUS_CHANGE,
            description=(
                f"Auto Navex sync: {n_flipped} unité(s) passées de {from_status} → {to_status} "
                f"pour la commande #{order.id} (bordereau {order.bordereau_barcode})"
            ),
            target_model="Order", target_id=order.id,
        )
    return n_flipped


def _shopify_get_access_token():
    """Exchange the Dev Dashboard Client ID + Client Secret for a short-lived
    Admin API access token via the OAuth client_credentials grant.

    Required env vars:
      - SHOPIFY_SHOP_DOMAIN (e.g. 'baratstunisia.myshopify.com')
      - SHOPIFY_CLIENT_ID
      - SHOPIFY_CLIENT_SECRET

    Returns (token: str, error_msg: str). One of the two is empty.
    Cached for ~50 minutes in module-level dict to avoid re-fetching on every call.
    """
    import urllib.request, urllib.parse, urllib.error, time
    domain = os.environ.get("SHOPIFY_SHOP_DOMAIN", "").strip()
    cid = os.environ.get("SHOPIFY_CLIENT_ID", "").strip()
    csecret = os.environ.get("SHOPIFY_CLIENT_SECRET", "").strip()
    if not (domain and cid and csecret):
        return "", "SHOPIFY_SHOP_DOMAIN / SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET non configurés."

    # Module-level cache to avoid OAuth roundtrip on every cancel
    global _SHOPIFY_TOKEN_CACHE
    try:
        cache = _SHOPIFY_TOKEN_CACHE
    except NameError:
        _SHOPIFY_TOKEN_CACHE = {}
        cache = _SHOPIFY_TOKEN_CACHE
    now = time.time()
    entry = cache.get(domain)
    if entry and entry.get("expires_at", 0) > now + 60:
        return entry["token"], ""

    url = f"https://{domain}/admin/oauth/access_token"
    body = urllib.parse.urlencode({
        "client_id": cid,
        "client_secret": csecret,
        "grant_type": "client_credentials",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            token = data.get("access_token") or ""
            # client_credentials grants are typically valid ~1 hour
            expires_in = int(data.get("expires_in") or 3600)
            cache[domain] = {"token": token, "expires_at": now + expires_in}
            return token, "" if token else "Pas de token dans la réponse Shopify."
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        return "", f"HTTP {e.code}: {raw[:300]}"
    except Exception as e:
        return "", f"Erreur réseau: {e}"


def _shopify_cancel_order(shopify_order_id):
    """Call Shopify Admin API to cancel an order.

    Requires env vars:
      - SHOPIFY_SHOP_DOMAIN (e.g. 'baratstunisia.myshopify.com')
      - SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET (preferred, OAuth flow)
      - OR SHOPIFY_ADMIN_API_TOKEN (legacy custom-app static token)

    Returns (ok: bool, response_data: dict|str).
    """
    import urllib.request, urllib.error
    domain = os.environ.get("SHOPIFY_SHOP_DOMAIN", "").strip()
    if not domain:
        return False, {"_error": "SHOPIFY_SHOP_DOMAIN non configuré."}
    if not shopify_order_id:
        return False, {"_error": "Shopify order ID vide."}

    # Get an access token. Prefer OAuth client_credentials (Dev Dashboard apps).
    token, err = _shopify_get_access_token()
    if not token:
        # Fallback: a directly-set token (custom apps héritées only)
        token = os.environ.get("SHOPIFY_ADMIN_API_TOKEN", "").strip()
    if not token:
        return False, {"_error": err or "Pas de token Shopify disponible."}

    # Strip any 'gid://shopify/Order/' prefix if present, keep only the numeric id
    sid = str(shopify_order_id).strip()
    if "/" in sid:
        sid = sid.rsplit("/", 1)[-1]

    url = f"https://{domain}/admin/api/2024-10/orders/{sid}/cancel.json"
    payload = json.dumps({
        "reason": "customer",
        "email": False,
        "restock": False,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Shopify-Access-Token", token)

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {"_raw": raw}
            return True, data
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"_raw": raw, "_http_status": e.code}
        return False, data
    except Exception as e:
        return False, {"_error": f"Erreur réseau: {e}"}


def _shopify_mark_paid(shopify_order_id, amount, currency="TND"):
    """Call Shopify Admin API to mark an order as paid by creating a
    successful transaction. Useful for Cash on Delivery (COD) orders that
    started as 'pending' — once we collect cash from Navex, we mirror that
    payment status to Shopify so it shows 'Paid' in the merchant dashboard.

    Note: this does NOT actually move money; it's a bookkeeping update.

    Returns (ok: bool, response_data: dict|str).
    """
    import urllib.request, urllib.error
    domain = os.environ.get("SHOPIFY_SHOP_DOMAIN", "").strip()
    if not domain:
        return False, {"_error": "SHOPIFY_SHOP_DOMAIN non configuré."}
    if not shopify_order_id:
        return False, {"_error": "Shopify order ID vide."}

    token, err = _shopify_get_access_token()
    if not token:
        token = os.environ.get("SHOPIFY_ADMIN_API_TOKEN", "").strip()
    if not token:
        return False, {"_error": err or "Pas de token Shopify disponible."}

    sid = str(shopify_order_id).strip()
    if "/" in sid:
        sid = sid.rsplit("/", 1)[-1]

    # POST /admin/api/2024-10/orders/{id}/transactions.json
    # kind="sale" + status="success" mark the order as Paid in Shopify.
    # gateway="manual" indicates the payment was collected outside Shopify
    # (e.g. cash via the delivery service).
    url = f"https://{domain}/admin/api/2024-10/orders/{sid}/transactions.json"
    body = json.dumps({
        "transaction": {
            "kind": "sale",
            "status": "success",
            "amount": str(amount),
            "currency": currency,
            "gateway": "manual",
        }
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Shopify-Access-Token", token)

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {"_raw": raw}
            return True, data
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"_raw": raw, "_http_status": e.code}
        return False, data
    except Exception as e:
        return False, {"_error": f"Erreur réseau: {e}"}


def _extract_shopify_order_id_from_notes(notes):
    """Helper to pull the Shopify order id from the Order.notes string we set
    when receiving the webhook (format: 'shopify_order_id=12345 | ...').
    Returns empty string if not found.
    """
    if not notes:
        return ""
    import re
    m = re.search(r"shopify_order_id=(\d+)", notes)
    return m.group(1) if m else ""


def _navex_cancel_colis(bordereau):
    """Call Navex's delete endpoint to cancel an existing colis.

    Returns (ok: bool, response_data: dict|str).
    On any error (network, refused), ok=False.
    Endpoint:
        POST https://app.navex.tn/api/rashop-delete-{TOKEN}/v1/post.php
        body: delete_code=<bordereau>
    """
    import urllib.request, urllib.parse
    token = os.environ.get("NAVEX_API_TOKEN", "")
    if not token:
        return False, {"_error": "NAVEX_API_TOKEN non configuré côté serveur."}
    if not bordereau:
        return False, {"_error": "Bordereau vide."}

    url = f"https://app.navex.tn/api/rashop-delete-{token}/v1/post.php"
    try:
        body = urllib.parse.urlencode({"delete_code": bordereau}).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {"_raw": raw}
    except Exception as e:
        return False, {"_error": f"Erreur réseau: {e}"}

    # Success: status==1 OR status_message looks positive (no "ERREUR")
    if isinstance(data, dict):
        status = data.get("status")
        msg = str(data.get("status_message") or data.get("message") or "")
        if status == 1 or status == "1" or status == "ok" or status == "success":
            return True, data
        if msg and "erreur" not in msg.lower() and "error" not in msg.lower():
            # Likely success even without explicit status==1
            return True, data
    return False, data


@csrf_exempt
@require_POST
@login_required(login_url="/login/")
def api_push_order_to_navex(request, pk):
    """Manual fallback: push a v2 Order to Navex.
    The recommended path is now via the 'Confirmée' status button which calls
    api_order_change_status — this endpoint stays as a manual fallback button.
    """
    from .models import Order
    if not _orders_role_check(request):
        return JsonResponse({"status": "error", "message": "Accès refusé."}, status=403)
    try:
        order = Order.objects.select_related("customer", "region", "sales_page").prefetch_related(
            "order_offers__lines__product", "order_offers__lines__variant",
            "lines__product", "lines__variant",
        ).get(pk=pk)
    except Order.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Commande introuvable."}, status=404)
    return _push_order_to_navex_internal(request, order)


@csrf_exempt
@require_POST
@login_required(login_url="/login/")
def api_sync_v2_orders_navex(request):
    """Manual button: sync all pending v2 orders' Navex status now."""
    if not _orders_role_check(request):
        return JsonResponse({"status": "error", "message": "Accès refusé."}, status=403)
    from .models import log_action, AuditLog
    n_attempted, n_updated = _sync_navex_for_v2_orders(only_pending=True)
    log_action(
        request.user, AuditLog.NAVEX_SYNC,
        description=f"Sync Navex v2: {n_updated}/{n_attempted} commandes mises à jour",
        request=request,
    )
    return JsonResponse({
        "status": "ok",
        "attempted": n_attempted,
        "updated": n_updated,
    })


@login_required(login_url="/login/")
def unit_detail(request, barcode):
    """Show the full history of a single ProductUnit (barcode)."""
    from .models import ProductUnit, StockMovement, OrderItem
    barcode = (barcode or "").strip().upper()
    try:
        unit = ProductUnit.objects.select_related("variant__product").get(barcode=barcode)
    except ProductUnit.DoesNotExist:
        return render(request, "inventory/unit_not_found.html", {"barcode": barcode}, status=404)

    movements = StockMovement.objects.filter(unit=unit).select_related("user").order_by("-moved_at")
    # Find all orders this unit appeared in (for context)
    order_items = OrderItem.objects.filter(unit=unit).select_related("order").order_by("-scanned_at")

    return render(request, "inventory/unit_detail.html", {
        "unit": unit,
        "movements": movements,
        "order_items": order_items,
    })


# ============================================================================
#                          META ADS SPENDING
# ============================================================================
# Reads ad spend data from Facebook Marketing API and combines it with our
# sales data to compute ROI. Requires env vars:
#   META_ACCESS_TOKEN  — long-lived access token with ads_read permission
#   META_AD_ACCOUNT_ID — ad account id (without 'act_' prefix)
# ============================================================================

def _meta_fetch_spend(start_date, end_date):
    """Fetch ad spend from Meta Marketing API between two dates (inclusive).
    Returns a dict {date_string: spend_amount} like {'2026-05-30': 12.50, ...}
    Returns empty dict on any error.
    """
    import urllib.request, urllib.parse, urllib.error
    token = os.environ.get("META_ACCESS_TOKEN", "").strip()
    accounts_raw = os.environ.get("META_AD_ACCOUNT_ID", "").strip()
    if not accounts_raw:
        return {}
    account_ids = [a.strip() for a in accounts_raw.split(",") if a.strip()]
    per_account = {}
    for pair in os.environ.get("META_AD_ACCOUNT_TOKENS", "").split(","):
        pair = pair.strip()
        if pair and ":" in pair:
            aid, _, tok = pair.partition(":")
            per_account[aid.strip().replace("act_", "")] = tok.strip()
    if not token and not per_account:
        return {}
    account_rates = {}
    for pair in os.environ.get("META_ACCOUNT_RATES", "").split(","):
        pair = pair.strip()
        if pair and ":" in pair:
            aid, _, rate = pair.partition(":")
            try:
                account_rates[aid.strip().replace("act_", "")] = float(rate.strip())
            except (ValueError, TypeError):
                pass
    result = {}
    for account_id in account_ids:
        bare = account_id.replace("act_", "")
        acc_token = per_account.get(bare, token)
        if not acc_token:
            continue
        rate = account_rates.get(bare, 1.0)
        acct = account_id if account_id.startswith("act_") else f"act_{account_id}"
        url = (
            f"https://graph.facebook.com/v18.0/{acct}/insights"
            f"?fields=spend"
            f"&time_range={{'since':'{start_date}','until':'{end_date}'}}"
            f"&time_increment=1"
            f"&access_token={acc_token}"
        )
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception:
            continue
        for entry in data.get("data", []):
            date_start = entry.get("date_start", "")
            try:
                spend = float(entry.get("spend", "0")) * rate
            except (ValueError, TypeError):
                spend = 0.0
            result[date_start] = result.get(date_start, 0.0) + spend
    return result


@login_required
def ads_dashboard(request):
    """Dashboard showing Meta ad spend vs sales revenue."""
    from datetime import date, timedelta
    today = date.today()
    first_of_month = today.replace(day=1)
    # Default: last 30 days
    start = today - timedelta(days=29)

    # Allow ?month=YYYY-MM filter
    month_filter = request.GET.get("month", "").strip()
    if month_filter:
        try:
            y, m = month_filter.split("-")
            start = date(int(y), int(m), 1)
            # End of month
            if int(m) == 12:
                end_of_month = date(int(y), 12, 31)
            else:
                end_of_month = date(int(y), int(m) + 1, 1) - timedelta(days=1)
            end_date = min(end_of_month, today)
        except Exception:
            end_date = today
    else:
        end_date = today

    start_str = start.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")

    # Fetch from Meta
    spend_by_day = _meta_fetch_spend(start_str, end_str)

    # Compute totals
    total_spend = sum(spend_by_day.values())
    today_spend = spend_by_day.get(today.strftime("%Y-%m-%d"), 0.0)
    month_spend = sum(
        v for d, v in spend_by_day.items()
        if d.startswith(today.strftime("%Y-%m"))
    )

    # Fetch sales (paid ShippingOrders) for the same range
    paid_orders = ShippingOrder.objects.filter(
        status=ShippingOrder.PAID,
        closed_at__date__gte=start,
        closed_at__date__lte=end_date,
    )
    total_revenue = sum(
        float(o.amount_collected or 0) for o in paid_orders
    )

    today_orders = paid_orders.filter(closed_at__date=today)
    today_revenue = sum(float(o.amount_collected or 0) for o in today_orders)

    month_orders = paid_orders.filter(closed_at__date__gte=first_of_month)
    month_revenue = sum(float(o.amount_collected or 0) for o in month_orders)

    # ROAS = revenue / spend (multiplier). ROI = (revenue - spend) / spend (%)
    def safe_div(a, b):
        return (a / b) if b else 0
    roas_total = safe_div(total_revenue, total_spend)
    roi_total = safe_div(total_revenue - total_spend, total_spend) * 100
    roas_today = safe_div(today_revenue, today_spend)
    roas_month = safe_div(month_revenue, month_spend)

    # Build daily rows for the table — combine spend + revenue per day
    revenue_by_day = {}
    for o in paid_orders:
        if not o.closed_at:
            continue
        d = o.closed_at.strftime("%Y-%m-%d")
        revenue_by_day[d] = revenue_by_day.get(d, 0) + float(o.amount_collected or 0)

    all_dates = sorted(set(list(spend_by_day.keys()) + list(revenue_by_day.keys())), reverse=True)
    rows = []
    for d in all_dates:
        s = spend_by_day.get(d, 0)
        r = revenue_by_day.get(d, 0)
        rows.append({
            "date": d,
            "spend": s,
            "revenue": r,
            "roas": safe_div(r, s),
            "profit": r - s,
        })

    has_token = bool(os.environ.get("META_ACCESS_TOKEN", "").strip())
    has_account = bool(os.environ.get("META_AD_ACCOUNT_ID", "").strip())

    return render(request, "inventory/ads_dashboard.html", {
        "rows": rows,
        "total_spend": total_spend,
        "total_revenue": total_revenue,
        "today_spend": today_spend,
        "today_revenue": today_revenue,
        "month_spend": month_spend,
        "month_revenue": month_revenue,
        "roas_total": roas_total,
        "roi_total": roi_total,
        "roas_today": roas_today,
        "roas_month": roas_month,
        "start": start_str,
        "end": end_str,
        "has_token": has_token,
        "has_account": has_account,
    })


# ===========================================================================
# Statistics — Commandes tab
# ===========================================================================
def _business_day_bounds(d, tz):
    """For a calendar date `d`, return the [start, end) datetimes of the
    business day that ENDS at 17:00 on `d` (runs from 17:00 the previous day
    to 17:00 on `d`). So an order at 2pm on `d` counts as `d`, but one after
    17:00 on `d` rolls into the next day."""
    import datetime as _dt
    end_naive = _dt.datetime.combine(d, _dt.time(17, 0))
    end = timezone.make_aware(end_naive, tz)
    start = end - _dt.timedelta(days=1)
    return start, end


@login_required(login_url="/login/")
def stats_commandes(request):
    """Statistics page — Commandes tab. Per business-day (17:00→17:00) counts
    of orders per status over a date range, with Min/Max/Avg/Somme and a
    percentage of Sortie (orders scanned into v1 shipping)."""
    if not request.user.is_superuser:
        return redirect("home")
    from .models import Order, ShippingOrder
    import datetime as _dt
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo("Africa/Tunis")
    except Exception:
        tz = timezone.get_current_timezone()

    # Parse date range (defaults: last 14 business days ending today).
    today = timezone.localdate()
    try:
        start_date = _dt.date.fromisoformat(request.GET.get("from", ""))
    except ValueError:
        start_date = today - _dt.timedelta(days=13)
    try:
        end_date = _dt.date.fromisoformat(request.GET.get("to", ""))
    except ValueError:
        end_date = today
    if start_date > end_date:
        start_date, end_date = end_date, start_date

    # Build list of business days in range.
    days = []
    d = start_date
    while d <= end_date:
        days.append(d)
        d += _dt.timedelta(days=1)

    # Overall window for one big query.
    win_start, _ = _business_day_bounds(start_date, tz)
    _, win_end = _business_day_bounds(end_date, tz)

    orders = (Order.objects.filter(created_at__gte=win_start, created_at__lt=win_end)
              .values("id", "status", "created_at", "exchange_of_id"))

    # Source filter (same grouping as the orders list).
    source_filter = request.GET.get("source", "all")
    if source_filter == "barats":
        orders = orders.filter(sales_page__name__iexact="Barats.tn")
    elif source_filter == "converty":
        orders = orders.filter(sales_page__name__iexact="Converty")
    elif source_filter == "facebook":
        orders = orders.exclude(sales_page__name__iexact="Barats.tn").exclude(sales_page__name__iexact="Converty")
    # Which orders are "Sortie" = have a linked v1 ShippingOrder.
    sortie_q = ShippingOrder.objects.filter(
        order__created_at__gte=win_start, order__created_at__lt=win_end
    )
    if source_filter == "barats":
        sortie_q = sortie_q.filter(order__sales_page__name__iexact="Barats.tn")
    elif source_filter == "converty":
        sortie_q = sortie_q.filter(order__sales_page__name__iexact="Converty")
    elif source_filter == "facebook":
        sortie_q = sortie_q.exclude(order__sales_page__name__iexact="Barats.tn").exclude(order__sales_page__name__iexact="Converty")
    sortie_ids = set(sortie_q.values_list("order_id", flat=True))

    # status row -> predicate
    def _row_for(o):
        rows = []
        st = o["status"]
        if o["exchange_of_id"]:
            rows.append("echange")
        if st == "returned":
            rows.append("retour")
        elif st in ("en_cours", "au_magasin"):
            rows.append("encours")
        elif st == "livree":
            rows.append("livree")
        elif st == "payee":
            rows.append("payee")
        elif st == "annulee":
            rows.append("annulee")
        if o["id"] in sortie_ids:
            rows.append("sortie")
        return rows

    ROW_KEYS = ["echange", "retour", "encours", "livree", "payee", "annulee", "sortie"]
    ROW_LABELS = {
        "echange": "Echange", "retour": "Retour", "encours": "En Cours",
        "livree": "Livrée", "payee": "Payée", "annulee": "Annulée", "sortie": "Sortie",
    }

    # daily[row][day] = count
    daily = {k: {dd: 0 for dd in days} for k in ROW_KEYS}
    tous_daily = {dd: 0 for dd in days}

    # Precompute day boundaries for fast bucketing.
    bounds = [( *(_business_day_bounds(dd, tz)), dd) for dd in days]

    for o in orders:
        ca = o["created_at"]
        # find the day bucket
        day = None
        for s, e, dd in bounds:
            if s <= ca < e:
                day = dd
                break
        if day is None:
            continue
        tous_daily[day] += 1
        for rk in _row_for(o):
            daily[rk][day] += 1

    def _agg(series):
        vals = list(series.values())
        s = sum(vals)
        mn = min(vals) if vals else 0
        mx = max(vals) if vals else 0
        avg = round(s / len(vals)) if vals else 0
        return {"min": mn, "max": mx, "avg": avg, "somme": s}

    tous_total = _agg(tous_daily)["somme"] or 0
    sortie_total = _agg(daily["sortie"])["somme"] or 0

    table = []
    for rk in ROW_KEYS:
        a = _agg(daily[rk])
        if rk in ("sortie", "annulee"):
            # Sortie (ship rate) and Annulée (never shipped) are vs ALL orders.
            denom = tous_total
        else:
            # Other statuses are outcomes of shipped orders -> vs Sortie.
            denom = sortie_total
        pct = (a["somme"] / denom * 100) if denom else 0
        table.append({
            "key": rk, "label": ROW_LABELS[rk],
            "min": a["min"], "max": a["max"], "avg": a["avg"], "somme": a["somme"],
            "pct": round(pct, 2),
        })
    # Tous row
    ta = _agg(tous_daily)
    table.append({
        "key": "tous", "label": "Tous",
        "min": ta["min"], "max": ta["max"], "avg": ta["avg"], "somme": ta["somme"],
        "pct": 100.0,
    })

    # Chart series (per day) as JSON.
    chart = {
        "labels": [dd.strftime("%d/%m") for dd in days],
        "series": {ROW_LABELS[rk]: [daily[rk][dd] for dd in days] for rk in ROW_KEYS},
    }

    return render(request, "inventory/stats_commandes.html", {
        "table": table,
        "chart_json": json.dumps(chart),
        "from_date": start_date.isoformat(),
        "to_date": end_date.isoformat(),
        "source_filter": source_filter,
        "n_days": len(days),
    })


# ---------------------------------------------------------------------------
# MESSENGER DM ORDER AUTOMATION
# Webhook receives Messenger messages + ad referral, stores the conversation,
# and (when complete) extracts an order via Gemini into a pending non_confirmee
# Order for human confirmation.
# ---------------------------------------------------------------------------
@csrf_exempt
def api_messenger_webhook(request):
    """Meta Messenger webhook.
      GET  → verification: echo hub.challenge if hub.verify_token matches.
      POST → message events: store messages + capture ad referral.
    """
    # --- GET: Meta verification handshake ---
    if request.method == "GET":
        verify_token = os.environ.get("MESSENGER_VERIFY_TOKEN", "")
        mode = request.GET.get("hub.mode")
        token = request.GET.get("hub.verify_token")
        challenge = request.GET.get("hub.challenge", "")
        if mode == "subscribe" and token and token == verify_token:
            return HttpResponse(challenge, content_type="text/plain")
        return HttpResponse("Verification failed", status=403)

    # --- POST: incoming events ---
    if request.method != "POST":
        return HttpResponse(status=405)

    from .models import MessengerConversation, log_action, AuditLog
    import json as _json
    try:
        payload = _json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"status": "ok"})  # ack anyway so Meta doesn't retry

    # TEMP DIAGNOSTIC: log the raw payload so we can see Instagram vs Messenger
    # structure. Remove once Instagram handling is confirmed.
    try:
        log_action(None, AuditLog.OTHER,
                   description="DM webhook RAW: " + _json.dumps(payload)[:1500])
    except Exception:
        pass

    try:
        obj_type = payload.get("object", "")
        # "instagram" => IG Direct; "page" => Facebook Messenger.
        platform = "instagram" if obj_type == "instagram" else "messenger"
        for entry in payload.get("entry", []):
            page_id = str(entry.get("id") or "")
            for ev in entry.get("messaging", []):
                _msg = ev.get("message") or {}
                is_echo = bool(_msg.get("is_echo"))
                # For a normal inbound message, the customer is the sender.
                # For an ECHO (a message the PAGE sent — including our own
                # auto-replies and staff replies), Meta reports sender=page and
                # recipient=customer. We flip so the conversation is keyed on the
                # customer and the message is stored as coming from the page.
                if is_echo:
                    customer_id = str((ev.get("recipient") or {}).get("id") or "")
                    sender_id = customer_id
                else:
                    sender_id = str((ev.get("sender") or {}).get("id") or "")
                if not sender_id or sender_id == page_id:
                    continue
                # Reuse the most recent conversation from THIS sender on THIS
                # page if it's less than 48h old — regardless of status — so a
                # customer who messages again (or re-sends their number) updates
                # the same order instead of spawning a duplicate. A different
                # page is treated as a separate conversation (different brand).
                from django.utils import timezone as _tzc
                from datetime import timedelta as _tdc
                cutoff = _tzc.now() - _tdc(hours=48)
                conv = (MessengerConversation.objects
                        .filter(sender_id=sender_id, page_id=page_id,
                                updated_at__gte=cutoff)
                        .order_by("-id").first())
                if conv is None:
                    if is_echo:
                        # An outgoing message with no existing conversation to
                        # attach to — nothing to record against, skip it.
                        continue
                    conv = MessengerConversation.objects.create(
                        sender_id=sender_id, page_id=page_id,
                        platform=platform,
                    )
                # Fetch the customer's profile name from Meta once, if we don't
                # have it yet — so orders get filled with the client's real name
                # even when they only sent a phone number. (Not for echoes.)
                if not is_echo and not conv.sender_name:
                    nm = _fetch_dm_sender_name(page_id, sender_id, platform)
                    if nm:
                        conv.sender_name = nm

                # Capture ad referral (attribution). Present on the first message
                # from an ad, or as a standalone 'referral' event. (Not echoes.)
                referral = None if is_echo else (ev.get("referral") or (ev.get("message") or {}).get("referral"))
                if referral:
                    conv.source_ad_id = str(referral.get("ad_id") or conv.source_ad_id or "")
                    conv.source_ad_ref = str(referral.get("ref") or conv.source_ad_ref or "")
                    conv.source_campaign = str(referral.get("ads_context_data", {}).get("ad_title")
                                               or conv.source_campaign or "")
                    conv.ctwa_clid = str(referral.get("ctwa_clid") or conv.ctwa_clid or "")
                    # Resolve the ad_id to its real Meta campaign name once, so
                    # the UI can show "Traffic | Jordan" instead of the post
                    # title. Best-effort; won't block if Meta is slow.
                    if conv.source_ad_id and not conv.source_campaign_name:
                        nm = _resolve_ad_campaign_name(conv.source_ad_id)
                        if nm:
                            conv.source_campaign_name = nm

                # Capture the message text. Dedupe by Meta's message id (mid):
                # Meta retries webhook deliveries, and without this the same
                # message gets appended many times.
                msg = ev.get("message") or {}
                text = msg.get("text")
                mid = msg.get("mid") or ""
                # Collect image attachment URLs. Meta sends them the same way for
                # Messenger and Instagram: message.attachments[] with
                # type == "image" and payload.url. Instagram customers often
                # send photos, so without this those messages looked empty.
                images = []
                for att in (msg.get("attachments") or []):
                    if (att.get("type") == "image"):
                        u = (att.get("payload") or {}).get("url")
                        if u:
                            images.append(u)
                if text or images:
                    msgs = conv.messages or []
                    already = mid and any(m.get("mid") == mid for m in msgs)
                    if not already:
                        msgs.append({
                            "from": "page" if is_echo else "user",
                            "text": text or "",
                            "images": images,
                            "ts": str(ev.get("timestamp") or ""),
                            "mid": mid,
                        })
                        conv.messages = msgs
                conv.save()

                # Everything below (profile name, ad referral, auto-reply,
                # extraction) is about the CUSTOMER's inbound messages. Skip it
                # entirely for echoes (our own outgoing messages).
                if is_echo:
                    continue

                # Send the one-time Arabic auto-reply ONLY once the customer has
                # sent a phone number — i.e. there's real order intent. Greeting
                # someone who only said "bonjour" or asked a price is premature.
                # We scan all accumulated message text for a Tunisian phone.
                conv_text_all = " ".join(
                    (m.get("text") or "") for m in (conv.messages or []))
                has_phone_now = bool(_extract_tn_phone(conv_text_all))

                # --- Auto-reply bot (Étape 1): answer questions in Tunisian
                # Arabic BEFORE a phone number arrives. Gated by env flag. The
                # bot steps aside once there's a phone (order intent) so staff /
                # the confirmation auto-reply take over. A per-conversation cap
                # prevents runaway loops.
                _bot_on = os.environ.get("AUTOREPLY_BOT_ENABLED", "").strip() in ("1", "true", "True")
                # Test mode: if AUTOREPLY_BOT_TEST_SENDER is set, the bot ONLY
                # replies to that one sender_id (your own account), so you can
                # safely try it live without answering real customers.
                _bot_test_sender = os.environ.get("AUTOREPLY_BOT_TEST_SENDER", "").strip()
                _is_test = bool(_bot_test_sender) and (str(sender_id) == _bot_test_sender)
                if _bot_test_sender:
                    _bot_on = _bot_on and _is_test
                # In test mode we relax the gates (reply even if a phone is
                # present or the conversation isn't NEW) so you can iterate. For
                # real customers, keep the safe gates: no phone yet, not already
                # greeted, and conversation still NEW.
                _gates_ok = (_is_test) or (
                    not has_phone_now and not conv.auto_replied
                    and conv.status == MessengerConversation.NEW)
                if _bot_on and _gates_ok and ((text or "").strip() or images):
                    # Dedup: Meta sometimes delivers the same webhook event more
                    # than once (retries / duplicate deliveries). Without a guard
                    # each delivery spawns a reply → the bot answers twice. Skip
                    # if we've already handled this incoming message id (mid).
                    _already_handled = False
                    try:
                        global _BOT_HANDLED_MIDS
                        try:
                            _BOT_HANDLED_MIDS
                        except NameError:
                            _BOT_HANDLED_MIDS = set()
                        _key = f"{conv.id}:{mid}" if mid else ""
                        if _key and _key in _BOT_HANDLED_MIDS:
                            _already_handled = True
                        elif _key:
                            _BOT_HANDLED_MIDS.add(_key)
                            # keep the set from growing unbounded
                            if len(_BOT_HANDLED_MIDS) > 5000:
                                _BOT_HANDLED_MIDS = set(
                                    list(_BOT_HANDLED_MIDS)[-2000:])
                    except Exception:
                        _already_handled = False
                    if _already_handled:
                        continue
                    _bot_count = 0
                    try:
                        _bot_count = sum(1 for m in (conv.messages or [])
                                         if m.get("from") == "page" and m.get("bot"))
                    except Exception:
                        _bot_count = 0
                    if _bot_count < (100 if _is_test else 5):  # cap bot turns
                        # The bot reply (download photo + build catalogue + Claude
                        # Vision) is too slow to run inline — Meta times out the
                        # webhook and the reply never sends. Run it in a background
                        # thread so we return to Meta immediately.
                        def _run_bot(conv_id, pg, sn, plat):
                            try:
                                from .models import MessengerConversation as _MC
                                import time as _time
                                _c = _MC.objects.filter(pk=conv_id).first()
                                if not _c:
                                    return
                                # Guard: if the bot already replied in the last
                                # 8 seconds, skip (protects against two near-
                                # simultaneous webhook events, e.g. text+image).
                                try:
                                    _last = 0.0
                                    for _m in reversed(_c.messages or []):
                                        if _m.get("from") == "page" and _m.get("bot"):
                                            _last = float(_m.get("bot_ts") or 0)
                                            break
                                    if _last and (_time.time() - _last) < 8:
                                        return
                                except Exception:
                                    pass
                                _rep = _bot_reply(_c)
                                if _rep and _messenger_send_text(pg, sn, _rep, plat):
                                    _c.refresh_from_db()
                                    _mm = _c.messages or []
                                    _mm.append({"from": "page", "text": _rep,
                                                "ts": "", "mid": "", "bot": True,
                                                "bot_ts": _time.time()})
                                    _c.messages = _mm
                                    _c.save(update_fields=["messages", "updated_at"])
                            except Exception:
                                pass
                        try:
                            import threading as _thr
                            _thr.Thread(target=_run_bot,
                                        args=(conv.id, page_id, sender_id, platform),
                                        daemon=True).start()
                        except Exception:
                            # Fallback: run inline if threads aren't available.
                            _reply = _bot_reply(conv)
                            if _reply and _messenger_send_text(page_id, sender_id, _reply, platform):
                                mm = conv.messages or []
                                mm.append({"from": "page", "text": _reply,
                                           "ts": "", "mid": "", "bot": True})
                                conv.messages = mm
                                conv.save(update_fields=["messages", "updated_at"])

                if not conv.auto_replied and has_phone_now:
                    from django.utils import timezone as _tz2
                    from datetime import timedelta as _td2
                    recently_greeted = (MessengerConversation.objects
                        .filter(sender_id=sender_id, page_id=page_id,
                                auto_replied=True,
                                updated_at__gte=_tz2.now() - _td2(hours=6))
                        .exclude(pk=conv.pk).exists())
                    if recently_greeted:
                        conv.auto_replied = True
                        conv.save(update_fields=["auto_replied", "updated_at"])
                    elif _messenger_send_text(page_id, sender_id, MESSENGER_AUTOREPLY_AR, platform):
                        conv.auto_replied = True
                        conv.save(update_fields=["auto_replied", "updated_at"])

                # B) Auto-extract when the conversation looks complete.
                try:
                    _try_extract_and_create_pending(conv)
                except Exception:
                    pass
    except Exception as e:
        try:
            log_action(None, AuditLog.OTHER,
                       description=f"Messenger webhook erreur: {str(e)[:300]}")
        except Exception:
            pass

    # Always 200 so Meta considers it delivered.
    return JsonResponse({"status": "ok"})


def _conversation_looks_complete(conv):
    """Cheap rule check: does the accumulated conversation look like a real
    order yet? We require (a) a Tunisian phone number (8 digits) somewhere, and
    (b) at least a few words of customer text (a product mention is likely).
    This gates the Gemini call so we don't extract on 'bonjour'."""
    import re
    text = " ".join(m.get("text", "") for m in (conv.messages or [])
                     if m.get("from") == "user")
    if not text:
        return False
    # A valid Tunisian mobile (proper prefix, not digits from a post URL).
    has_phone = bool(_extract_tn_phone(text))
    # A phone number alone is enough — staff finish the rest, so we never miss
    # an order. (Previously also required 4+ words, which dropped phone-only DMs.)
    return has_phone


def _extract_order_from_conversation(conv):
    """Send the conversation to Gemini and parse a structured order JSON.
    Returns a dict or None. Tuned for Tunisian Arabic / French / English."""
    import json as _json
    convo_text = "\n".join(
        f"{'CLIENT' if m.get('from') == 'user' else 'PAGE'}: {m.get('text','')}"
        for m in (conv.messages or []) if m.get("text")
    )
    if not convo_text.strip():
        return None
    prompt = (
        "Tu es un assistant qui extrait une commande à partir d'une conversation "
        "Messenger d'une boutique de vêtements tunisienne. La conversation est en "
        "arabe tunisien, français ou anglais (souvent mélangés).\n\n"
        "Lis la conversation et réponds UNIQUEMENT avec un objet JSON valide, sans "
        "texte avant ou après, sans backticks. Schéma:\n"
        "{\n"
        '  "is_order": true/false,        // false si ce n\'est pas une vraie commande\n'
        '  "customer_name": "",\n'
        '  "phone": "",                    // 8 chiffres tunisiens\n'
        '  "city": "",                     // gouvernorat/ville de livraison\n'
        '  "address": "",\n'
        '  "items": [\n'
        '    {"product": "", "color": "", "size": "", "qty": 1}\n'
        "  ]\n"
        "}\n\n"
        "Règles: si une info manque, mets une chaîne vide \"\" (ou 1 pour qty). "
        "Le PAGE (vendeur) propose souvent les produits et prix (ex: 'Pull 59dt, "
        "Ensemble 99dt'); le CLIENT choisit ensuite. Extrais le produit que le "
        "CLIENT veut réellement commander d'après tout l'échange, même si c'est "
        "le PAGE qui a nommé le produit. Pour l'adresse/ville, utilise ce "
        "qu'écrit le CLIENT (gouvernorat tunisien). Ne devine pas un produit qui "
        "n'est mentionné nulle part.\n\n"
        "Conversation:\n" + convo_text
    )
    raw = _gemini_generate(prompt, max_tokens=800, temperature=0.0)
    if not raw:
        return None
    # Strip accidental code fences.
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    cleaned = cleaned.strip()
    try:
        data = _json.loads(cleaned)
    except Exception:
        # Try to salvage the first {...} block.
        import re
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not m:
            return None
        try:
            data = _json.loads(m.group(0))
        except Exception:
            return None
    return data


def _web_resolve_tn_locality(text, all_regions, all_delegations, norm_fn):
    """Use Claude+web search to find which Tunisian governorate (and delegation)
    a locality belongs to, then match it back to our Region/Delegation objects.
    Returns (Region|None, Delegation|None). Best-effort, never raises."""
    import re as _re
    if not text:
        return None, None
    region_names = ", ".join(r.name for r in all_regions)
    prompt = (
        "Tu es un expert de la géographie tunisienne. Voici une adresse écrite "
        "par un client (souvent en arabe ou arabizi) :\n\n"
        f"\"{text[:300]}\"\n\n"
        "Cherche sur le web à quel GOUVERNORAT (région) tunisien appartient la "
        "localité principale de cette adresse. Puis réponds UNIQUEMENT avec une "
        "seule ligne au format exact :\n"
        "GOUVERNORAT: <nom en français> | DELEGATION: <nom en français ou NONE>\n\n"
        "Le gouvernorat DOIT être l'un de ceux-ci : " + region_names + "\n"
        "Aucune explication, aucune analyse, juste la ligne."
    )
    resp = _claude_web_search(prompt, max_tokens=1500)
    if not resp:
        return None, None
    gm = _re.search(r"GOUVERNORAT\s*[:\-]?\s*([^\|\n]+)", resp, _re.IGNORECASE)
    dm = _re.search(r"DELEGATION\s*[:\-]?\s*([^\|\n]+)", resp, _re.IGNORECASE)
    if not gm:
        return None, None
    gov = gm.group(1).strip(" :-|")
    dele = dm.group(1).strip(" :-|") if dm else ""
    if dele.upper() == "NONE":
        dele = ""
    # Match governorate to a Region.
    region_match = None
    gn = norm_fn(gov)
    for r in all_regions:
        rn = norm_fn(r.name)
        if rn == gn or (gn and (gn in rn or rn in gn)):
            region_match = r
            break
    if not region_match:
        return None, None
    # Match delegation within that region.
    ville_match = None
    if dele:
        dn = norm_fn(dele)
        for d in all_delegations:
            if d.region_id == region_match.id:
                ddn = norm_fn(d.name)
                if ddn == dn or (dn and (dn in ddn or ddn in dn)):
                    ville_match = d
                    break
    return region_match, ville_match


def _resolve_region_for_order(order, conv=None, force=False):
    """Match an order's free-text address/city to a Region (gouvernorat) and
    Delegation (ville) from our list, using Gemini. Fills order.region and
    order.ville if a confident match is found. Best-effort; no-op on failure.

    Used for Messenger orders whose address arrives as free Tunisian text
    (e.g. 'korba (sidi Ali)') rather than structured Shopify fields. If the
    order's address field is empty, falls back to scanning the conversation.

    `force=True` re-resolves even if a region is already set — used on the DM
    path, where the order-creation step assigns a weak/wrong region guess that
    this (stronger, conversation-aware) resolver should override."""
    import re as _re
    from .models import Region, Delegation
    if not order:
        return
    if order.region_id and not force:
        return  # already has a region and we're not forcing an override
    addr = (order.address or "").strip()
    ville = (order.ville or "").strip()
    # Fallback: if no address on the order, scan the conversation for an
    # "adresse: ..." line (customers often label it explicitly).
    convo_text = ""
    if conv is not None and conv.messages:
        convo_text = " ".join(m.get("text", "") for m in conv.messages
                              if m.get("from") == "user")
    if not addr:
        # Capture the address after an "adresse:" label, stopping before the
        # next labeled field (numéro/téléphone/couleur/taille) or end.
        m = _re.search(
            r"(?:adresse|3adress|address|عنوان)\s*[:\-]?\s*"
            r"(.+?)(?=\s*(?:num[ée]ro|t[ée]l|phone|couleur|color|taille|size|\d{8})|$)",
            convo_text, _re.IGNORECASE)
        if m:
            addr = m.group(1).strip(" :-")[:120]
            if addr and not (order.address or "").strip():
                order.address = addr
                try:
                    order.save(update_fields=["address", "updated_at"])
                except Exception:
                    pass
    # If still no explicit address but we have conversation text, use the full
    # customer text as the matching input — the city is often inline (e.g.
    # 'Ensemble Taille L Gabés 23467059'). The anti-hallucination guard below
    # still verifies the match against this same text.
    if not addr and not ville and convo_text.strip():
        # Use a generous slice — in long exchange chats the address line can
        # appear well past the first few hundred chars (we saw 'سليانة' at ~char
        # 600 in a 1200-char conversation). Cap high enough not to lose it.
        addr = convo_text.strip()[:2000]
    if not addr and not ville:
        return
    all_regions = list(Region.objects.filter(is_active=True))
    all_delegations = list(Delegation.objects.filter(is_active=True).select_related("region"))
    if not all_regions:
        return

    def _norm(s):
        import unicodedata as _ud
        s = (s or "").lower().strip()
        # Strip accents (Gabés -> gabes) for robust matching.
        s = "".join(c for c in _ud.normalize("NFD", s)
                    if _ud.category(c) != "Mn")
        # Fold common Tunisian transliteration variants so phonetic spellings
        # (English-style) match our French-style DB names:
        #   sh <-> ch   (sharada = cherarda),  ou -> u,  y -> i,
        #   double letters -> single,  q/k unified,  '3'->'a'(ع),  drop apostrophes
        s = s.replace("sh", "ch")
        s = s.replace("ou", "u").replace("aa", "a")
        s = _re.sub(r"[qk]+", "k", s)
        s = _re.sub(r"[^a-z0-9\u0600-\u06FF]+", "", s)
        s = _re.sub(r"(.)\1+", r"\1", s)  # collapse repeated letters
        return s

    # FAST PATH (no AI call): if the extracted city/address already contains a
    # delegation name that matches ours exactly (after normalization), use it
    # directly. This skips the transliteration + pick LLM calls on the common
    # case where extraction already gave a clean city (e.g. 'Gabes', 'Sousse'),
    # which is the bulk of orders — big cost saving.
    def _norm_fast(s):
        import unicodedata as _ud
        s = (s or "").lower().strip()
        s = "".join(c for c in _ud.normalize("NFD", s) if _ud.category(c) != "Mn")
        s = s.replace("sh", "ch").replace("ou", "u").replace("aa", "a")
        s = _re.sub(r"[qk]+", "k", s)
        s = _re.sub(r"[^a-z0-9]+", "", s)
        s = _re.sub(r"(.)\1+", r"\1", s)
        return s
    _ville_norm = _norm_fast(ville)
    _addr_words = set(_re.split(r"\s+", (addr or "")))
    if _ville_norm or _addr_words:
        for d in all_delegations:
            dn = _norm_fast(d.name)
            if not dn or len(dn) < 4:
                continue
            hit = (dn == _ville_norm) or any(_norm_fast(w) == dn for w in _addr_words if len(w) >= 4)
            if hit:
                order.region = d.region
                order.ville = d.name
                try:
                    order.save(update_fields=["region", "ville", "updated_at"])
                except Exception:
                    pass
                return

    # DIRECT DELEGATION HIT via a single LLM call: the customer often writes the
    # exact delegation name in Arabic (e.g. 'الشراردة' = Cherarda). Rather than
    # fight phonetic spelling variants (sharada vs cherarda) with fuzzy string
    # matching, we ask the model to read the address and pick the matching
    # delegation NAME straight from our list. This is reliable across scripts.
    src_for_hit = (addr or "") + " " + (ville or "") + " " + (convo_text or "")
    has_arabic_src = any("\u0600" <= ch <= "\u06FF" for ch in src_for_hit)
    if has_arabic_src or True:
        # Use the tail (address usually near the end, after the phone).
        tail = src_for_hit[-900:] if len(src_for_hit) > 900 else src_for_hit
        all_dele_names = sorted({d.name for d in all_delegations})
        # The delegation list is identical on every call → send it as a cached
        # prefix (Anthropic prompt caching) so we don't pay full price each time.
        cached_list = ("Liste EXACTE des délégations tunisiennes :\n"
                       + ", ".join(all_dele_names))
        hit_prompt = (
            "Voici la fin d'une conversation client (arabe/arabizi) contenant une "
            "adresse de livraison tunisienne :\n\n\"" + ((addr or "")[:200] + " " + tail) + "\"\n\n"
            "Parmi la liste EXACTE de délégations ci-dessus, laquelle correspond "
            "à la localité de livraison ? Translitère mentalement l'arabe "
            "(ex: الشراردة = Cherarda, القيروان = Kairouan).\n"
            "RÈGLE : cherche dans TOUT le texte un mot qui EST une délégation de "
            "la liste. Un nom de délégation présent dans la liste prime sur une "
            "petite localité inconnue. Ne te laisse PAS piéger par un mot comme "
            "'بئر' (Bir) au début : par exemple 'بئر الوصفان الشراردة' → la "
            "délégation est 'الشراردة' = Cherarda (Kairouan), PAS un 'Bir...' "
            "d'un autre gouvernorat.\n"
            "Réponds UNIQUEMENT avec le nom EXACT de la délégation tel qu'il "
            "figure dans la liste, ou 'NONE'."
        )
        pick = (_claude_generate(hit_prompt, max_tokens=120, temperature=0.0,
                                 cached_prefix=cached_list) or "").strip()
        # Claude sometimes replies verbosely ("En analysant... **Cherarda**")
        # despite instructions. Don't just take the first line — scan the whole
        # reply for any delegation name from our list (longest match wins).
        pick_norm = _norm(pick)
        chosen = None
        if pick_norm and pick_norm != _norm("NONE"):
            for d in all_delegations:
                dn = _norm(d.name)
                if len(dn) >= 4 and dn in pick_norm:
                    if chosen is None or len(dn) > len(_norm(chosen.name)):
                        chosen = d
        if chosen is not None:
            order.region = chosen.region
            order.ville = chosen.name
            try:
                order.save(update_fields=["region", "ville", "updated_at"])
            except Exception:
                pass
            return

    options_lines = []
    for r in all_regions:
        r_dlgs = [d for d in all_delegations if d.region_id == r.id]
        if r_dlgs:
            options_lines.append(f"{r.name}: " + ", ".join(d.name for d in r_dlgs))
        else:
            options_lines.append(r.name)
    if not options_lines:
        return

    prompt = (
        "IMPORTANT : ta réponse doit être UNE SEULE LIGNE au format exact "
        "'REGION: nom | VILLE: nom' (ou 'NONE'). AUCUNE analyse, AUCUN titre, "
        "AUCUNE explication, AUCun markdown. Juste la ligne finale.\n\n"
        "Tu matches une adresse tunisienne à notre liste de gouvernorats "
        "(régions) et délégations (villes). La liste est en LETTRES LATINES mais "
        "le client écrit souvent en ARABE. Tu DOIS translittérer mentalement "
        "l'arabe en latin pour trouver la correspondance.\n"
        "Exemples de translittération : الشراردة = Cherarda, القيروان = Kairouan, "
        "صفاقس = Sfax, سوسة = Sousse, نابل = Nabeul, بنزرت = Bizerte, "
        "المهدية = Mahdia, قابس = Gabes, الشابة = Chebba.\n"
        "RÈGLE PRINCIPALE : cherche dans TOUT le texte (après translittération) "
        "N'IMPORTE QUEL mot qui correspond à une délégation de la liste — même "
        "s'il n'est pas le premier lieu mentionné. Une délégation qui figure "
        "dans la liste prime TOUJOURS sur une petite localité inconnue.\n"
        "Exemple concret : 'بئر الوصفان الشراردة' → 'بئر الوصفان' (Bir El Wesfen) "
        "est une petite localité inconnue, mais 'الشراردة' = Cherarda EST une "
        "délégation de Kairouan → réponds REGION: Kairouan | VILLE: Cherarda. "
        "Ne choisis PAS un 'Bir...' d'un autre gouvernorat juste à cause du mot "
        "'بئر'.\n"
        "Si tu reconnais le GOUVERNORAT mais pas la délégation exacte, donne "
        "quand même la REGION et mets VILLE: NONE. "
        "Ne réponds NONE complet que si AUCUN lieu tunisien n'est mentionné. "
        "Les Tunisiens utilisent des chiffres (3=ع, 5=خ, 7=ح, 9=ق, 2=ء). "
        "Tu DOIS choisir des noms EXACTS de la liste (en latin). "
        "Réponds UNIQUEMENT : 'REGION: nom | VILLE: nom' ou 'NONE'.\n\n"
        f"Texte du client (peut contenir la localité parmi d'autres infos) : {addr} {ville}\n\n"
        "Liste des régions et délégations :\n" + "\n".join(options_lines)
        + "\n\nRéponse :"
    )
    resp = _gemini_generate(prompt, max_tokens=512, temperature=0.0,
                            model="gemini-2.5-flash")
    if not resp or resp.strip().upper() == "NONE":
        return
    # Parse REGION and VILLE independently so a partial/varied response still
    # works (e.g. 'REGION: Siliana' with no ville, or different separators).
    rm = _re.search(r"REGION\s*[:\-]?\s*([^\|\n]+)", resp, _re.IGNORECASE)
    vm = _re.search(r"VILLE\s*[:\-]?\s*([^\|\n]+)", resp, _re.IGNORECASE)
    if not rm:
        # Verbose reply without the strict format: try to recover by finding a
        # delegation name that appears BOTH in the model's response and in the
        # customer's text (after normalization). This catches cases where the
        # model transliterated correctly but wrapped it in analysis prose.
        resp_norm = _norm(resp)
        cust_norm = _norm((addr or "") + " " + (ville or "") + " " + (convo_text or ""))
        for d in all_delegations:
            dn = _norm(d.name)
            if dn and len(dn) >= 4 and dn in resp_norm and dn in cust_norm:
                order.region = d.region
                order.ville = d.name
                try:
                    order.save(update_fields=["region", "ville", "updated_at"])
                except Exception:
                    pass
                return
        return
    region_name = rm.group(1).strip(" :-|")
    ville_name = vm.group(1).strip(" :-|") if vm else ""
    if region_name.upper() == "NONE":
        return
    if ville_name.upper() == "NONE":
        ville_name = ""
    if not region_name:
        return
    # Resolve region (exact then normalized contains).
    region_match = None
    for r in all_regions:
        if r.name.lower() == region_name.lower():
            region_match = r
            break
    if not region_match:
        rn = _norm(region_name)
        for r in all_regions:
            if rn and (_norm(r.name) == rn or rn in _norm(r.name) or _norm(r.name) in rn):
                region_match = r
                break
    if not region_match:
        return
    # Resolve delegation within the region.
    ville_match = None
    for d in all_delegations:
        if d.region_id == region_match.id and d.name.lower() == ville_name.lower():
            ville_match = d
            break
    if not ville_match:
        vn = _norm(ville_name)
        for d in all_delegations:
            if d.region_id == region_match.id and vn and (_norm(d.name) == vn or vn in _norm(d.name) or _norm(d.name) in vn):
                ville_match = d
                break

    # ANTI-HALLUCINATION GUARD: only accept the match if the matched region or
    # delegation name actually appears (fuzzy) in what the customer wrote.
    # Gemini sometimes invents a region (e.g. defaults to 'Sfax/Jebeniana') when
    # the address is vague or absent — this rejects those.
    source_text = _norm((addr or "") + " " + (ville or "") + " " + (convo_text or ""))
    def _appears(name):
        n = _norm(name)
        if not n or len(n) < 3:
            return False
        if n in source_text:
            return True
        # Match on the DISTINCTIVE PREFIX of the name (first 4-5 chars) to allow
        # spelling/vowel variants while avoiding false hits on shared suffixes.
        prefix = n[:5] if len(n) >= 5 else n
        if prefix in source_text:
            return True
        if len(n) >= 4 and n[:4] in source_text:
            return True
        # Fuzzy: scan words in the source text for a near-match (<=2 edits) to
        # the name — catches vowel variants like 'seliana'~'siliana' while still
        # rejecting totally different names ('gabes' vs 'jebeniana').
        def _lev(a, b):
            if abs(len(a) - len(b)) > 2:
                return 99
            prev = list(range(len(b) + 1))
            for i, ca in enumerate(a, 1):
                cur = [i]
                for j, cb in enumerate(b, 1):
                    cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                                   prev[j - 1] + (ca != cb)))
                prev = cur
            return prev[-1]
        for word in _re.split(r"\s+", (addr or "") + " " + (ville or "") + " " + (convo_text or "")):
            wn = _norm(word)
            if len(wn) >= 5 and abs(len(wn) - len(n)) <= 2 and _lev(wn, n) <= 2:
                return True
        return False
    region_ok = _appears(region_match.name)
    ville_ok = ville_match and _appears(ville_match.name)
    # Script-mismatch escape: the guard does a character match, which fails when
    # the customer writes in Arabic (المنزه) but our region names are in Latin
    # (El Menzah) — different scripts can't be compared char-by-char. In that
    # case trust flash (it's reliable cross-script) instead of rejecting.
    src_all = (addr or "") + " " + (ville or "") + " " + (convo_text or "")
    has_arabic = any("\u0600" <= ch <= "\u06FF" for ch in src_all)
    names_latin = all(ord(ch) < 128 for ch in (region_match.name + (ville_match.name if ville_match else "")))
    script_mismatch = has_arabic and names_latin
    if not region_ok and not ville_ok and not script_mismatch:
        # The list-based match looks hallucinated. A web search can identify the
        # locality's real governorate, but it uses Sonnet + web search which is
        # ~10-20x the cost of a Haiku call. Off by default — enable only if you
        # want to spend for edge cases: set ANTHROPIC_ENABLE_WEB_SEARCH=1.
        if os.environ.get("ANTHROPIC_ENABLE_WEB_SEARCH", "").strip() in ("1", "true", "True"):
            web_region, web_ville = _web_resolve_tn_locality(
                addr or convo_text, all_regions, all_delegations, _norm)
            if web_region:
                order.region = web_region
                if web_ville:
                    order.ville = web_ville.name
                fields = ["region", "updated_at"] + (["ville"] if web_ville else [])
                try:
                    order.save(update_fields=fields)
                except Exception:
                    pass
        return

    order.region = region_match
    if ville_match:
        order.ville = ville_match.name
    fields = ["region", "updated_at"]
    if ville_match:
        fields.append("ville")
    try:
        order.save(update_fields=fields)
    except Exception:
        pass


def _build_shopify_shape_from_extraction(data, conv):
    """Convert Gemini's extracted order dict into the Shopify-shaped payload
    that _create_order_from_shopify_shaped_payload expects."""
    items = data.get("items") or []
    line_items = []
    for it in items:
        prod = (it.get("product") or "").strip()
        if not prod:
            continue
        color = (it.get("color") or "").strip()
        size = (it.get("size") or "").strip()
        vt_parts = [p for p in (size, color) if p]
        line_items.append({
            "title": prod,
            "name": prod,
            "variant_title": " / ".join(vt_parts),
            "properties": [
                {"name": "couleur", "value": color} if color else {"name": "", "value": ""},
            ],
            "quantity": int(it.get("qty") or 1),
            "price": "0",
            "sku": "",
        })
    return {
        "id": f"dm_{conv.id}",
        "order_number": f"DM{conv.id}",
        "name": f"DM{conv.id}",
        "shipping_address": {
            "phone": data.get("phone") or "",
            "name": data.get("customer_name") or conv.sender_name or "",
            "city": data.get("city") or "",
            "address1": data.get("address") or "",
        },
        "customer": {"phone": data.get("phone") or "",
                     "first_name": data.get("customer_name") or conv.sender_name or ""},
        "phone": data.get("phone") or "",
        "line_items": line_items,
        "shipping_lines": [],
    }


def _fill_color_size_from_text(order, conv):
    """Scan the chat for COLOR and SIZE words and apply them to order lines that
    are still missing a variant/size. Lets a later message like 'noir taille L'
    complete a Pull Camo line that was added earlier."""
    from .models import ProductVariant
    import re as _re
    text = " ".join(m.get("text", "") for m in (conv.messages or [])
                    if m.get("from") == "user").lower()
    if not text:
        return

    # Color aliases (FR/EN) → match against each product's variants.
    color_aliases = {
        "noir": ["noir", "black", "noire"],
        "blanc": ["blanc", "white", "blanche"],
        "rouge": ["rouge", "red"],
        "bleu": ["bleu", "blue", "blue"],
        "vert": ["vert", "green"],
        "gris": ["gris", "gray", "grey"],
        "jaune": ["jaune", "yellow"],
        "rose": ["rose", "pink"],
        "beige": ["beige"],
        "marron": ["marron", "brown"],
        "orange": ["orange"],
        "violet": ["violet", "purple"],
    }
    # Which color words appear in the chat?
    mentioned_colors = []
    for canon, words in color_aliases.items():
        if any(_re.search(r"\b" + _re.escape(w) + r"\b", text) for w in words):
            mentioned_colors.append((canon, words))

    # Detect a size token (S/M/L/XL/XXL/2XL/3XL or a 1-2 digit number after
    # "taille"/"size" or standalone).
    size_found = ""
    msize = _re.search(r"\b(taille|size)\s*[:\-]?\s*([a-z0-9]{1,3})\b", text)
    if msize:
        size_found = msize.group(2).upper()
    if not size_found:
        m2 = _re.search(r"\b(xs|s|m|l|xl|xxl|xxxl|2xl|3xl|4xl)\b", text)
        if m2:
            size_found = m2.group(1).upper()
    # Sizes are stored as NUMBERS (1=S, 2=M, 3=L, 4=XL, 5=XXL). Convert any
    # letter size the customer used (e.g. "taille L" -> "3").
    if size_found:
        letter_to_number = {
            "XS": "1", "S": "1", "M": "2", "L": "3",
            "XL": "4", "XXL": "5", "2XL": "5", "XXXL": "5", "3XL": "5",
        }
        size_found = letter_to_number.get(size_found, size_found)

    changed = False
    for line in order.lines.all():
        if not line.product:
            continue
        # Fill VARIANT (color) if missing and a matching color was mentioned.
        if line.variant is None and mentioned_colors:
            for canon, words in mentioned_colors:
                variant = None
                for v in line.product.variants.all():
                    lbl = (v.color_label or "").strip().lower()
                    cn = (v.color_name or "").strip().lower()
                    if lbl in words or cn in words or canon in (lbl, cn):
                        variant = v
                        break
                if variant:
                    line.variant = variant
                    changed = True
                    break
        # Fill SIZE if missing.
        if not (line.size or "").strip() and size_found:
            line.size = size_found
            changed = True
        if changed:
            line.save(update_fields=["variant", "size"])
            changed = False


def _match_offers_from_text(order, conv):
    """Gemini-independent: scan the raw conversation text for any active offer
    or product NAME and add matches to the order. Works even when Gemini is
    down/empty. Catches loose mentions like 'pull camo' → offer 'Pull Camo'."""
    from .models import Offer, Product, OrderOffer, OrderLine
    text = " ".join(m.get("text", "") for m in (conv.messages or [])
                    if m.get("from") == "user").lower()
    if not text:
        return
    existing_names = set()
    for oo in order.order_offers.all():
        existing_names.add((oo.offer_name or "").strip().lower())
    for l in order.lines.filter(order_offer__isnull=True):
        if l.product:
            existing_names.add(l.product.name.strip().lower())

    # Active offers, longest name first (so "Ensemble Camo ZR" wins over "Camo").
    offers = sorted(Offer.objects.filter(is_active=True),
                    key=lambda o: len(o.name or ""), reverse=True)
    for offer in offers:
        nm = (offer.name or "").strip().lower()
        if not nm or nm in existing_names:
            continue
        if nm in text:
            oo = OrderOffer.objects.create(
                order=order, offer=offer, offer_name=offer.name,
                bundle_price=offer.price_for_page(order.sales_page), quantity=1,
            )
            for op in offer.products.all():
                OrderLine.objects.create(
                    order=order, order_offer=oo, product=op.product,
                    variant=None, size="", quantity=op.quantity, unit_price=0,
                )
            existing_names.add(nm)
    try:
        order.recalc_total()
        order.save(update_fields=["total", "updated_at"])
    except Exception:
        pass


def _add_extracted_items_to_order(order, data):
    """Add products/offers from a Gemini extraction to an existing pending
    order, skipping items already present. Matches each product name against
    active offers first, then products. Best-effort; staff finalize."""
    from .models import Offer, Product, OrderOffer, OrderLine
    items = data.get("items") or []
    if not items:
        return
    existing_names = set()
    for oo in order.order_offers.all():
        existing_names.add((oo.offer_name or "").strip().lower())
    for l in order.lines.filter(order_offer__isnull=True):
        if l.product:
            existing_names.add(l.product.name.strip().lower())

    for it in items:
        pname = (it.get("product") or "").strip()
        if not pname or pname.lower() in existing_names:
            continue
        qty = int(it.get("qty") or 1)
        offer = Offer.objects.filter(name__iexact=pname, is_active=True).first()
        if offer:
            for _ in range(max(qty, 1)):
                oo = OrderOffer.objects.create(
                    order=order, offer=offer, offer_name=offer.name,
                    bundle_price=offer.price_for_page(order.sales_page), quantity=1,
                )
                for op in offer.products.all():
                    OrderLine.objects.create(
                        order=order, order_offer=oo, product=op.product,
                        variant=None, size=(it.get("size") or ""),
                        quantity=op.quantity, unit_price=0,
                    )
            existing_names.add(pname.lower())
            continue
        product = Product.objects.filter(name__iexact=pname).first()
        if product:
            OrderLine.objects.create(
                order=order, product=product, variant=None,
                size=(it.get("size") or ""), quantity=qty, unit_price=0,
            )
            existing_names.add(pname.lower())
    try:
        order.recalc_total()
        order.save(update_fields=["total", "updated_at"])
    except Exception:
        pass


def _messenger_poll_page(page_id, limit=25):
    """Poll a single page's recent Messenger conversations via the Graph API
    and feed new messages into the same pipeline as the webhook. Works in
    Development mode with the page's own token — no App Review needed.

    Returns (conversations_seen, messages_added)."""
    import urllib.request as _ureq
    import urllib.parse as _uparse
    import json as _json
    from .models import MessengerConversation

    token = _messenger_page_token(page_id)
    if not token:
        return (0, 0)

    # Fetch conversations with their messages and ad referral context.
    fields = (
        "id,updated_time,"
        "messages.limit(15){id,message,from,created_time,attachments{image_data,mime_type,name,file_url}},"
        "participants"
    )
    base = (f"https://graph.facebook.com/v25.0/{page_id}/conversations"
            f"?platform=messenger&fields={_uparse.quote(fields)}"
            f"&limit={int(limit)}&access_token={_uparse.quote(token, safe='')}")
    try:
        with _ureq.urlopen(base, timeout=8) as resp:
            data = _json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception:
        return (0, 0)

    convs_seen = 0
    msgs_added = 0
    for thread in data.get("data", []):
        convs_seen += 1
        # Identify the customer participant (the one that isn't the page).
        sender_id = ""
        sender_name = ""
        for part in (thread.get("participants", {}) or {}).get("data", []):
            if str(part.get("id")) != str(page_id):
                sender_id = str(part.get("id") or "")
                sender_name = part.get("name") or ""
                break
        if not sender_id:
            continue

        # Reuse the same active-conversation lookup as the webhook.
        conv = (MessengerConversation.objects
                .filter(sender_id=sender_id, page_id=page_id,
                        status__in=[MessengerConversation.NEW,
                                    MessengerConversation.EXTRACTED])
                .order_by("-id").first())
        if conv is None:
            conv = MessengerConversation.objects.create(
                sender_id=sender_id, page_id=page_id, platform="messenger",
                sender_name=sender_name,
            )
        elif sender_name and not conv.sender_name:
            conv.sender_name = sender_name

        # Messages come newest-first from the API; insert oldest-first.
        msg_nodes = list(reversed((thread.get("messages", {}) or {}).get("data", [])))
        existing_mids = {m.get("mid") for m in (conv.messages or [])}
        msgs = conv.messages or []
        added_this_conv = 0
        for mn in msg_nodes:
            mid = mn.get("id") or ""
            text = mn.get("message") or ""
            frm = (mn.get("from") or {}).get("id")
            # Collect attachment image URLs (photos, stickers, shared posts).
            img_urls = []
            for att in ((mn.get("attachments") or {}).get("data") or []):
                url = (att.get("image_data") or {}).get("url") or att.get("file_url") or ""
                mime = att.get("mime_type") or ""
                if url and (mime.startswith("image") or not mime):
                    img_urls.append(url)
            # Skip only if there's NOTHING (no text and no image) or already seen.
            if (not text and not img_urls) or (mid and mid in existing_mids):
                continue
            is_page = (str(frm) == str(page_id))
            msgs.append({
                "from": "page" if is_page else "user",
                "text": text,
                "images": img_urls,
                "ts": mn.get("created_time", ""), "mid": mid,
            })
            existing_mids.add(mid)
            msgs_added += 1
            if not is_page:
                added_this_conv += 1
        conv.messages = msgs
        conv.save()

        # Only run the (slow) extraction pipeline if we actually added a new
        # customer message this poll — avoids re-processing old conversations
        # on every poll and keeps the request fast.
        if added_this_conv:
            try:
                _try_extract_and_create_pending(conv, skip_gemini=True)
            except Exception:
                pass

    return (convs_seen, msgs_added)


def _messenger_enrich_settled(batch=4, quiet_minutes=5):
    """Deferred Gemini pass: for conversations that have gone quiet (customer
    stopped messaging) and whose order still lacks address/products, run Gemini
    ONCE to extract address/region + product/offer from the FULL conversation
    (both customer and page messages). Processes a small batch to avoid the
    Gemini rate-limit / worker-timeout issues that bulk extraction caused.

    "Quiet" is measured from the NEWEST message's real timestamp (not the row's
    updated_at, which the poll refreshes every cycle).

    Returns the number of conversations enriched."""
    from .models import MessengerConversation, Order
    from django.utils import timezone as _tz
    from datetime import timedelta as _td
    from django.utils.dateparse import parse_datetime as _pdt

    now = _tz.now()
    cutoff = now - _td(minutes=quiet_minutes)
    floor = now - _td(hours=48)

    # Candidate rows: not yet enriched, pending non_confirmee order, created in
    # the last 48h. We then check each one's last-message timestamp in Python.
    candidates = (MessengerConversation.objects
                  .filter(gemini_enriched=False,
                          pending_order__isnull=False,
                          pending_order__status=Order.NON_CONFIRMEE,
                          created_at__gte=floor)
                  .order_by("created_at"))

    enriched = 0
    for conv in candidates:
        if enriched >= batch:
            break
        # Find the newest message timestamp in the stored messages.
        last_ts = None
        for m in (conv.messages or []):
            ts = m.get("ts") or ""
            dt = _pdt(ts) if ts else None
            if dt is not None:
                if _tz.is_naive(dt):
                    dt = _tz.make_aware(dt, _tz.get_default_timezone())
                if last_ts is None or dt > last_ts:
                    last_ts = dt
        # If we couldn't read any message timestamp, fall back to updated_at.
        effective = last_ts or conv.updated_at
        # Skip if the conversation is still active (last message < quiet window).
        if effective and effective > cutoff:
            continue

        ok = False
        try:
            # Pre-check Gemini so we only mark enriched when it actually worked.
            data = _extract_order_from_conversation(conv)
            if data:
                conv.extracted = data
                # Pass the data we already extracted so we don't call Gemini twice.
                _try_extract_and_create_pending(conv, pre_data=data)
                ok = True
        except Exception:
            ok = False
        # Only mark enriched if Gemini succeeded — otherwise leave it for a later
        # pass (e.g. after a rate-limit window clears) instead of giving up.
        if ok:
            conv.gemini_enriched = True
            try:
                conv.save(update_fields=["gemini_enriched", "updated_at"])
            except Exception:
                pass
            enriched += 1
    return enriched


@login_required(login_url="/login/")
def api_messenger_poll(request):
    """Manually trigger a poll of all configured pages. Returns a summary.
    Call this on a schedule (cron/Railway) or from a button to pull new DMs
    without relying on the webhook (works in Development mode)."""
    if not _orders_role_check(request):
        return JsonResponse({"status": "error"}, status=403)
    results = {}
    total_msgs = 0
    for page_id in MESSENGER_PAGE_TO_SALESPAGE.keys():
        seen, added = _messenger_poll_page(page_id)
        results[page_id] = {"conversations": seen, "new_messages": added}
        total_msgs += added
    enriched = _messenger_enrich_settled()
    return JsonResponse({"status": "ok", "total_new_messages": total_msgs,
                         "enriched": enriched, "pages": results})


@csrf_exempt
def api_messenger_poll_cron(request):
    """Token-protected poll endpoint for an EXTERNAL scheduler (cron-job.org,
    etc.). No login needed — protected by a secret token that must match the
    MESSENGER_POLL_TOKEN env var. Call as:
        /api/messenger/poll-cron/?token=YOUR_SECRET
    """
    secret = os.environ.get("MESSENGER_POLL_TOKEN", "")
    given = request.GET.get("token", "")
    if not secret or given != secret:
        return JsonResponse({"status": "error", "message": "forbidden"}, status=403)
    total_msgs = 0
    for page_id in MESSENGER_PAGE_TO_SALESPAGE.keys():
        try:
            _seen, added = _messenger_poll_page(page_id)
            total_msgs += added
        except Exception:
            pass
    try:
        _messenger_enrich_settled()
    except Exception:
        pass

    # Catch-up: some conversations end up complete (customer sent a phone) but
    # were never turned into an order — e.g. the AI call failed transiently at
    # the moment the number arrived, leaving status=NEW with no pending order.
    # Nothing retries them otherwise, so the order is silently lost. Re-run
    # extraction here for recent, complete, order-less conversations.
    retried = 0
    try:
        from django.utils import timezone as _tzc
        from datetime import timedelta as _tdc
        cutoff = _tzc.now() - _tdc(hours=72)
        stuck = (MessengerConversation.objects
                 .filter(status=MessengerConversation.NEW,
                         pending_order_id__isnull=True,
                         updated_at__gte=cutoff)
                 .order_by("-id")[:40])
        for conv in stuck:
            try:
                if _conversation_looks_complete(conv):
                    _try_extract_and_create_pending(conv)
                    conv.refresh_from_db()
                    if conv.pending_order_id:
                        retried += 1
            except Exception:
                pass
    except Exception:
        pass

    return JsonResponse({"status": "ok", "total_new_messages": total_msgs,
                         "recovered_orders": retried})


def _try_extract_and_create_pending(conv, skip_gemini=False, pre_data=None):
    """When a phone number appears, create a pending non_confirmee Order even if
    product/size/address are still missing — staff finish the rest so no order
    is ever missed. We still run Gemini to pre-fill whatever it can extract.

    Guard rails:
      - Once the linked order is CONFIRMED (or beyond non_confirmee), we stop
        touching it — staff have taken over.
      - Once the conversation is older than 48h, we stop auto-updating (stale).
    """
    from .models import MessengerConversation, Order, Ad
    from django.utils import timezone as _tz
    from datetime import timedelta as _td
    import re as _re
    if conv.status not in (MessengerConversation.NEW, MessengerConversation.EXTRACTED):
        return
    if not _conversation_looks_complete(conv):
        return

    # Guard: if the linked order is no longer non_confirmee (staff confirmed /
    # pushed it), do not modify anything further.
    if conv.pending_order_id:
        po = conv.pending_order
        if po and po.status != Order.NON_CONFIRMEE:
            conv.status = MessengerConversation.CONFIRMED
            conv.save(update_fields=["status", "updated_at"])
            return

    # Guard: ignore conversations older than 48h (stale — staff should handle
    # any late follow-up manually rather than auto-editing an old order).
    if conv.created_at and (_tz.now() - conv.created_at) > _td(hours=48):
        return

    if pre_data is not None:
        data = pre_data or {}
    else:
        data = ({} if skip_gemini else _extract_order_from_conversation(conv)) or {}
    conv.extracted = data

    # Link the source ad if we can match the campaign/ad to a known Ad row.
    if conv.source_campaign and not conv.matched_ad_id:
        ad = Ad.objects.filter(campaign_name__iexact=conv.source_campaign).first()
        if ad:
            conv.matched_ad = ad

    # Build the payload from whatever Gemini extracted (may have no line items).
    shaped = _build_shopify_shape_from_extraction(data, conv)

    # Ensure we have a phone even if Gemini missed it: pull the first 8-digit run
    # from the conversation text directly.
    if not shaped.get("phone"):
        text = " ".join(m.get("text", "") for m in (conv.messages or [])
                        if m.get("from") == "user")
        ph = _extract_tn_phone(text)
        if ph:
            shaped["phone"] = ph
            shaped["shipping_address"]["phone"] = ph
            shaped["customer"]["phone"] = ph

    # A phone is the ONLY hard requirement now. No phone → can't create a usable
    # pending order, so just keep the conversation for later.
    if not shaped.get("phone"):
        conv.save(update_fields=["extracted", "matched_ad", "updated_at"])
        return

    # Route to the sales_page mapped from the Facebook Page this DM came from.
    sp_id = MESSENGER_PAGE_TO_SALESPAGE.get(str(conv.page_id or ""), MESSENGER_DEFAULT_SALESPAGE)

    # If a pending order ALREADY exists for this conversation (the customer is
    # sending info across several messages), UPDATE it instead of creating a
    # duplicate: refresh the saved chat and add any newly-mentioned products
    # that aren't on the order yet.
    if conv.pending_order_id:
        from django.utils import timezone as _tz
        order = conv.pending_order
        if order and order.status == Order.NON_CONFIRMEE:
            convo_text = "\n".join(
                f"{'Client' if m.get('from') == 'user' else 'Page'}: {m.get('text','')}"
                for m in (conv.messages or []) if m.get("text")
            )
            order.conversation_text = convo_text
            order.conversation_updated_at = _tz.now()
            # Fill address/name if newly available and still empty on the order.
            if data.get("address") and not (order.address or "").strip():
                order.address = data.get("address")
            order.save(update_fields=["conversation_text", "conversation_updated_at",
                                      "address", "updated_at"])
            # Resolve region/ville from the address text against our list, with
            # an anti-hallucination guard (verified against the conversation).
            # We do NOT blindly trust Gemini's raw 'city' — the resolver checks
            # the matched name actually appears in what the customer wrote.
            try:
                _resolve_region_for_order(order, conv=conv, force=True)
            except Exception:
                pass
            # Add newly-extracted offers/products not already present.
            try:
                _add_extracted_items_to_order(order, data)
            except Exception:
                pass
            # Gemini-independent: also match offers directly from chat text.
            try:
                _match_offers_from_text(order, conv)
            except Exception:
                pass
            # Fill color/size on lines from later messages (e.g. "noir taille L").
            try:
                _fill_color_size_from_text(order, conv)
            except Exception:
                pass
            conv.save(update_fields=["extracted", "matched_ad", "updated_at"])
            return

    # BUSINESS RULE: a customer may only have ONE active order at a time. A new
    # order is allowed only once every previous order is finished (delivered,
    # paid, returned, or cancelled). If this customer (matched by phone) still
    # has an in-flight order, do NOT create a duplicate — attach the new message
    # to that existing order's conversation instead and stop.
    _phone = shaped.get("phone") or ""
    if _phone:
        from .models import Customer
        _FINISHED = (Order.LIVREE, Order.PAYEE, Order.RETURNED,
                     Order.RETURNING, Order.ANNULEE, Order.SUPPRIME_NAVEX)
        _cust = Customer.objects.filter(phone=_phone).first()
        if _cust:
            _active = (Order.objects.filter(customer=_cust)
                       .exclude(status__in=_FINISHED)
                       .order_by("-id").first())
            if _active and _active.id != conv.pending_order_id:
                # Link this conversation to the existing active order so staff
                # see the new messages there, but don't create a second order.
                try:
                    conv.pending_order = _active
                    if _active.status == Order.NON_CONFIRMEE:
                        from django.utils import timezone as _tz
                        convo_text = "\n".join(
                            f"{'Client' if m.get('from') == 'user' else 'Page'}: {m.get('text','')}"
                            for m in (conv.messages or []) if m.get("text"))
                        _active.conversation_text = convo_text
                        _active.conversation_updated_at = _tz.now()
                        _active.save(update_fields=["conversation_text",
                                                    "conversation_updated_at", "updated_at"])
                    conv.save(update_fields=["pending_order", "extracted", "updated_at"])
                except Exception:
                    pass
                return

    try:
        _dm_source = "instagram" if (conv.platform == "instagram") else "messenger"
        _create_order_from_shopify_shaped_payload(
            shaped, source=_dm_source, external_id=f"dm_{conv.id}",
            sales_page_id=sp_id,
        )
        # Find the order we just created (external id stored in notes).
        order = Order.objects.filter(
            notes__contains=f"shopify_order_id=dm_{conv.id}"
        ).order_by("-id").first()
        if order:
            # Integrate with the EXISTING conversation system: store the chat on
            # the order (so the 💬 modal shows it), link the PSID on the customer,
            # and record the ad source in the notes.
            from django.utils import timezone as _tz
            convo_text = "\n".join(
                f"{'Client' if m.get('from') == 'user' else 'Page'}: {m.get('text','')}"
                for m in (conv.messages or []) if m.get("text")
            )
            order.conversation_text = convo_text
            order.conversation_updated_at = _tz.now()
            extra_notes = ["[Commande créée depuis Messenger]"]
            if conv.source_ad_id:
                extra_notes.append(f"Ad source: {conv.source_ad_id}")
            if conv.source_campaign:
                extra_notes.append(f"Campagne: {conv.source_campaign}")
            order.notes = (order.notes + "\n" + "\n".join(extra_notes)).strip()
            order.save(update_fields=["conversation_text", "conversation_updated_at",
                                      "notes", "updated_at"])
            # Link the PSID to the customer for future message matching.
            if order.customer and conv.sender_id and not order.customer.customer_psid:
                order.customer.customer_psid = conv.sender_id
                order.customer.save(update_fields=["customer_psid"])
            conv.pending_order = order
            conv.status = MessengerConversation.EXTRACTED
            # Gemini-independent: match offers directly from the chat text so a
            # mention like "pull camo" adds the offer even if Gemini returned {}.
            try:
                _match_offers_from_text(order, conv)
            except Exception:
                pass
            try:
                _fill_color_size_from_text(order, conv)
            except Exception:
                pass
    except Exception:
        pass
    conv.save(update_fields=["extracted", "matched_ad", "pending_order",
                             "status", "updated_at"])


@csrf_exempt
def privacy_policy(request):
    """Public privacy policy page (no auth) — required for Meta App Review and
    for handling customer data received via Messenger."""
    html = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Politique de confidentialité — Barats</title>
<style>
  body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:760px;
       margin:40px auto;padding:0 20px;line-height:1.6;color:#1a1a1a;}
  h1{font-size:28px;margin-bottom:4px;}
  h2{font-size:19px;margin-top:28px;}
  .updated{color:#666;font-size:14px;margin-bottom:24px;}
  a{color:#2563eb;}
</style>
</head>
<body>
<h1>Politique de confidentialité</h1>
<div class="updated">Dernière mise à jour : juin 2026</div>

<p>Barats (« nous ») exploite une boutique de vêtements en Tunisie avec
livraison à domicile (paiement à la livraison). Cette politique explique
quelles données nous collectons, comment nous les utilisons et les droits
dont vous disposez.</p>

<h2>1. Données que nous collectons</h2>
<p>Lorsque vous passez une commande — sur notre site web, par message sur nos
pages Facebook/Messenger ou Instagram, ou par téléphone — nous pouvons
collecter :</p>
<ul>
  <li>Votre nom</li>
  <li>Votre numéro de téléphone</li>
  <li>Votre adresse de livraison (gouvernorat, ville, adresse)</li>
  <li>Les détails de votre commande (articles, tailles, couleurs, quantités)</li>
  <li>Le contenu des messages que vous nous envoyez concernant votre commande</li>
</ul>

<h2>2. Comment nous utilisons vos données</h2>
<p>Nous utilisons ces informations uniquement pour :</p>
<ul>
  <li>Préparer, confirmer et livrer votre commande</li>
  <li>Vous contacter au sujet de votre commande</li>
  <li>Gérer les retours, échanges et le service après-vente</li>
  <li>Tenir notre registre de commandes et notre gestion de stock</li>
</ul>

<h2>3. Partage des données</h2>
<p>Nous ne vendons jamais vos données. Nous les partageons uniquement avec
notre transporteur de livraison afin d'acheminer votre colis. Nous pouvons
traiter les messages de commande à l'aide d'outils automatisés pour préparer
votre commande, sans usage publicitaire.</p>

<h2>4. Conservation</h2>
<p>Nous conservons les données de commande aussi longtemps que nécessaire pour
le suivi des livraisons, la comptabilité et le service client, puis nous les
supprimons ou les anonymisons.</p>

<h2>5. Vos droits</h2>
<p>Vous pouvez nous demander d'accéder à vos données, de les corriger ou de les
supprimer. Pour toute demande, contactez-nous via notre page Facebook ou par
message Messenger.</p>

<h2>6. Données Messenger / Instagram / Facebook</h2>
<p>Si vous nous contactez via Messenger ou Instagram, nous recevons votre
message et un identifiant de messagerie afin de traiter votre commande. Ces
informations ne sont utilisées que pour le traitement de votre commande et ne
sont pas utilisées à des fins publicitaires ni partagées avec des tiers autres
que notre transporteur.</p>

<h2>7. Contact</h2>
<p>Pour toute question concernant cette politique ou vos données, contactez-nous
via l'une de nos pages Facebook ou Instagram (Barats, Arrow Sportswear, Next
Generation, Handsome Collection, PrimeFit, Traffic).</p>

</body>
</html>"""
    return HttpResponse(html)
