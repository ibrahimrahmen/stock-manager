"""
Status flow:
  in_stock → shipped → paid
                ↓
             returned  (counts as available stock, can be shipped again)

Rules:
  - Only in_stock or returned units can be added to a shipping order
  - returned cannot go directly to paid
  - Scan stock = create new unit as in_stock
  - Scan return = mark shipped/paid unit as returned
"""

from django.utils import timezone
from django.db import transaction

from .models import (
    Product, ProductVariant, ProductUnit,
    ShippingOrder, OrderItem, StockMovement,
)
from .barcode_parser import parse_barcode, is_bordereau_barcode


def handle_shipping_scan(barcode: str, user=None) -> dict:
    barcode = barcode.strip().upper()
    if is_bordereau_barcode(barcode):
        return _handle_bordereau(barcode, user=user)
    return _handle_unit_scan(barcode, user=user)


def _get_navex_info(barcode: str):
    """Fetch full info from Navex getattente — short timeout, never blocks scan."""
    try:
        import os
        import urllib.request, urllib.parse
        navex_url = (
            f"https://app.navex.tn/api/rashop-etat-"
            f"{os.environ.get('NAVEX_API_TOKEN', '')}/v1/post.php"
        )
        data = urllib.parse.urlencode({"getattente": "1"}).encode()
        req = urllib.request.Request(
            navex_url,
            data=data, method="POST"
        )
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=3) as resp:  # 3s max — never blocks
            import json as j
            navex = j.loads(resp.read().decode())
        for colis in navex.get("colis", []):
            if colis.get("code_barre") == barcode:
                return {
                    "prix": colis.get("prix"),
                    "designation": colis.get("designation", ""),
                    "nom": colis.get("nom", "") or colis.get("client_nom", "") or colis.get("name", ""),
                    "tel": colis.get("tel", "") or colis.get("phone", "") or colis.get("telephone", ""),
                    "adresse": colis.get("adresse", "") or colis.get("address", ""),
                    "ville": colis.get("ville", "") or colis.get("city", ""),
                }
    except Exception:
        pass  # Never fail — scan works even without Navex
    return {}


def _matched_products_from_order(order) -> list:
    """Build prediction cards directly from a v2 Order's real line items.
    This is the ACCURATE source (no fuzzy designation parsing) — products,
    colors and sizes come straight from what the customer ordered.

    Returns the same card shape as _get_matched_products so the scan page can
    render them identically. Each line's quantity produces that many cards.
    """
    try:
        from .models import ProductUnit
        cards = []
        STATUS_TO_COLOR = {
            "exact": "green", "plus1": "green", "minus1": "yellow",
            "plus2": "orange", "minus2": "red", "none": "red",
        }

        # Build the list of (product, variant, size, qty) the order expects.
        # Prefer explicit lines; but an offer/ensemble often has NO standalone
        # lines — its pieces live in OfferProduct. Expand those so every piece
        # of the bundle is predicted (e.g. Ensemble ICY MAZE -> Pants + Shirt),
        # which lets the scan flag an incomplete bundle.
        expected = []  # list of dicts: product, variant, size, qty
        for line in order.lines.all():
            if line.product:
                expected.append({
                    "product": line.product, "variant": line.variant,
                    "size": (line.size or "").strip(), "qty": line.quantity or 1,
                })
        if not expected:
            # No standalone lines — expand the offers into component products.
            try:
                from .models import OfferProduct
                for oo in order.order_offers.all():
                    off = oo.offer
                    oq = oo.quantity or 1
                    if not off:
                        continue
                    for op in OfferProduct.objects.filter(offer=off).select_related("product"):
                        if not op.product:
                            continue
                        expected.append({
                            "product": op.product, "variant": None,
                            "size": "", "qty": (getattr(op, "quantity", 1) or 1) * oq,
                        })
            except Exception:
                pass

        for exp in expected:
            product = exp["product"]
            variant = exp["variant"]
            size = exp["size"]
            qty = exp["qty"]
            # Count in-stock units for this product/variant/size.
            in_stock_count = 0
            try:
                qs = ProductUnit.objects.filter(status=ProductUnit.IN_STOCK)
                if variant is not None:
                    qs = qs.filter(variant=variant)
                else:
                    qs = qs.filter(variant__product=product)
                if size:
                    qs = qs.filter(size=size)
                in_stock_count = qs.count()
            except Exception:
                in_stock_count = 0
            stock_status = "exact" if in_stock_count >= qty else "none"
            badge_color = STATUS_TO_COLOR.get(stock_status, "red")
            img = None
            try:
                if variant is not None and variant.image:
                    img = variant.image.url
            except Exception:
                img = None
            # One card per unit ordered (qty), so the warehouse scans each.
            for _ in range(qty):
                cards.append({
                    "id": product.id,
                    "name": product.name,
                    "code": getattr(product, "code", "") or "",
                    "color_matched": variant.color_label if variant else "",
                    "image_url": img,
                    "size": size,
                    "in_stock": in_stock_count,
                    "stock_ok": badge_color == "green",
                    "stock_status": stock_status,
                    "badge_color": badge_color,
                    "found_size": size,
                })
        return cards
    except Exception:
        return []


def _get_matched_products(designation: str) -> list:
    """Match products from a Navex designation string.
    Creates one card per product+color item in the designation.

    For each card we also include:
      - size: extracted from "(M)", "(L)" etc. in the designation
      - in_stock: number of physical units currently in_stock for that
        (product, variant, size) combo. 0 means not shippable.
    """
    if not designation:
        return []
    try:
        from .models import Product, ProductUnit
        import re
        COLOR_MAP = {
            "noir": "black", "black": "black",
            "blanc": "white", "white": "white",
            "bleu": "blue", "blue": "blue",
            "gris": "gray", "grey": "gray", "gray": "gray",
            "rouge": "red", "red": "red",
            "vert": "green", "green": "green",
            "rose": "pink", "pink": "pink",
            "jaune": "yellow", "yellow": "yellow",
            "orange": "orange",
            "beige": "beige",
            "marron": "brown", "brown": "brown",
            "burgundy": "burgundy", "bordeaux": "burgundy",
            "kaki": "khaki", "khaki": "khaki",
            "marine": "navy", "navy": "navy",
            "violet": "purple", "purple": "purple", "mauve": "purple",
        }

        # Split by comma — each part is one item e.g. "Pull Camo #0326 noir (M)"
        items = [part.strip() for part in designation.split(",")]
        # Remove the shop prefix (e.g. "92023 Barats.tn | Pull Camo...")
        cleaned_items = []
        for item in items:
            if "|" in item:
                item = item.split("|", 1)[1].strip()
            cleaned_items.append(item)

        # Sort products by name length DESCENDING so longest names match first.
        # This way "Polo Ling Hiver" wins over "Polo Ling" if both fit in the item.
        # For V2/V3 products sharing a parent, all are searched independently —
        # the candidate_variants logic later expands the stock search to siblings.
        products = sorted(
            Product.objects.prefetch_related("variants").all(),
            key=lambda p: len(p.name),
            reverse=True,
        )
        matched = []
        for item in cleaned_items:
            item_lower = item.lower()
            # Find which product this item refers to (longest match wins)
            matched_product = None
            for product in products:
                if product.name.lower() in item_lower:
                    matched_product = product
                    break
            if not matched_product:
                continue

            # Find color in this item — use word boundaries so "bleu" doesn't
            # match inside "blueline" (product name). The color word must be
            # surrounded by spaces, punctuation, or string edges.
            # IMPORTANT: when multiple colors are present (e.g. "blanc/noir"),
            # we pick the one that appears FIRST in the text (leftmost position),
            # not the first one in dict order.
            matched_variant = None
            # Find ALL color matches with their position in the text
            color_matches = []
            for fr, en in COLOR_MAP.items():
                pattern = r"\b" + re.escape(fr) + r"\b"
                m = re.search(pattern, item_lower)
                if m:
                    color_matches.append((m.start(), fr, en))
            # Sort by position in text (leftmost first)
            color_matches.sort(key=lambda x: x[0])

            for _, fr, en in color_matches:
                # Match using color_label (e.g. "BLACK", "WHITE", "BLUE")
                all_variants = list(matched_product.variants.all())
                en_norm = en.lower().strip()
                if en_norm == "grey": en_norm = "gray"
                for v in all_variants:
                    if v.color_label.lower().strip() == en_norm:
                        matched_variant = v
                        break
                if not matched_variant:
                    for v in all_variants:
                        if en_norm in v.color_label.lower():
                            matched_variant = v
                            break
                if matched_variant:
                    break

            if not matched_variant:
                matched_variant = matched_product.variants.first()

            # Extract size from parentheses, e.g. "(M)", "(XL)", "(36)"
            size_match = re.search(r"\(([^)]+)\)", item)
            extracted_size = size_match.group(1).strip().upper() if size_match else ""

            # Map standard sizes <-> numeric (internal storage uses numeric).
            # S=1, M=2, L=3, XL=4, XXL=5
            SIZE_TO_NUMERIC = {"S": 1, "M": 2, "L": 3, "XL": 4, "XXL": 5}
            NUMERIC_TO_SIZE = {v: k for k, v in SIZE_TO_NUMERIC.items()}

            # Normalize the requested size to a numeric "index" we can math on
            requested_idx = None
            if extracted_size in SIZE_TO_NUMERIC:
                requested_idx = SIZE_TO_NUMERIC[extracted_size]
            else:
                try:
                    requested_idx = int(extracted_size)
                except ValueError:
                    requested_idx = None

            def all_size_forms(idx):
                """Return all possible string forms for a numeric size index."""
                forms = [str(idx)]
                if idx in NUMERIC_TO_SIZE:
                    forms.append(NUMERIC_TO_SIZE[idx])
                return forms

            # Count IN_STOCK for a given variant + size index.
            # Returns (count, size_label_actually_found).
            def count_stock_for_size(variant, idx):
                if idx is None or idx <= 0:
                    return 0, ""
                forms = all_size_forms(idx)
                for sz in forms:
                    n = ProductUnit.objects.filter(
                        variant=variant,
                        status=ProductUnit.IN_STOCK,
                        size__iexact=sz,
                    ).count()
                    if n > 0:
                        return n, sz
                return 0, ""

            # Find ALL variants of this product with the same color_label
            # (handles duplicates), then for each one apply the size-tolerance rule.
            in_stock_count = 0
            stock_status = "none"   # 'exact', 'plus1', 'minus1', 'plus2', 'minus2', 'none'
            found_size_label = ""
            actual_variant_used = matched_variant

            if matched_variant:
                # Get all candidate variants: the matched one, plus any others
                # with the same color_label on the SAME product (handles duplicate
                # variants per color), PLUS any variants on parent/sibling products
                # (V2/V3 versions sharing the same physical SKU).
                candidate_variants = [matched_variant]

                # Build a list of "related" products: this one's parent, this one's
                # children (versions), and parent's other children (siblings).
                related_products = [matched_product]
                if matched_product.parent_product_id:
                    related_products.append(matched_product.parent_product)
                    # Add siblings (other children of the same parent)
                    for sibling in matched_product.parent_product.versions.all():
                        if sibling.id != matched_product.id:
                            related_products.append(sibling)
                else:
                    # This product might BE a parent. Add its children too.
                    for child in matched_product.versions.all():
                        related_products.append(child)

                # Now find variants with matching color_label across all related products
                target_color = (matched_variant.color_label or "").lower().strip()
                for related in related_products:
                    for v in related.variants.all():
                        if v.id == matched_variant.id:
                            continue
                        if target_color and v.color_label.lower().strip() == target_color:
                            candidate_variants.append(v)

                # Priority order: exact, +1, -1, +2, -2
                # We check across all candidate variants for each level before moving on.
                if requested_idx is not None and extracted_size:
                    priority_levels = [
                        ("exact",  requested_idx),
                        ("plus1",  requested_idx + 1),
                        ("minus1", requested_idx - 1),
                        ("plus2",  requested_idx + 2),
                        ("minus2", requested_idx - 2),
                    ]
                    for level_name, level_idx in priority_levels:
                        for v in candidate_variants:
                            n, label = count_stock_for_size(v, level_idx)
                            if n > 0:
                                in_stock_count = n
                                stock_status = level_name
                                found_size_label = label
                                actual_variant_used = v
                                break
                        if in_stock_count > 0:
                            break
                else:
                    # No size in designation — just count whatever's in stock
                    for v in candidate_variants:
                        n = ProductUnit.objects.filter(
                            variant=v, status=ProductUnit.IN_STOCK
                        ).count()
                        if n > 0:
                            in_stock_count = n
                            stock_status = "exact"  # no size constraint = exact
                            actual_variant_used = v
                            break

            # Map status to a badge color (frontend will color accordingly)
            STATUS_TO_COLOR = {
                "exact":  "green",
                "plus1":  "green",
                "minus1": "yellow",
                "plus2":  "orange",
                "minus2": "red",
                "none":   "red",
            }
            badge_color = STATUS_TO_COLOR.get(stock_status, "red")

            matched.append({
                "id": matched_product.id,
                "name": matched_product.name,
                "code": matched_product.code,
                "color_matched": actual_variant_used.color_label if actual_variant_used else "",
                "image_url": actual_variant_used.image.url if actual_variant_used and actual_variant_used.image else None,
                "size": extracted_size,
                "in_stock": in_stock_count,
                "stock_ok": badge_color == "green",
                "stock_status": stock_status,   # exact/plus1/minus1/plus2/minus2/none
                "badge_color": badge_color,     # green/yellow/orange/red
                "found_size": found_size_label, # what size we actually found (could be different from requested)
            })

        return matched
    except Exception:
        return []


def _handle_bordereau(barcode: str, user=None) -> dict:
    closed_order = None
    incomplete_warning = None

    with transaction.atomic():
        # Close any open order first
        for order in ShippingOrder.objects.filter(status=ShippingOrder.OPEN):
            if order.items.count() == 0:
                return {
                    "status": "error",
                    "message": f"Impossible de fermer l'ordre {order.bordereau_barcode} — aucune unité scannée !",
                    "code": "EMPTY_ORDER",
                }
            # Bundle-completeness check: compare the number of scanned units to
            # the number of pieces the order's offers expect. If they differ
            # (e.g. an Ensemble with 2 pieces but only 1 scanned), warn — the
            # colis is likely incomplete. We warn rather than block so staff can
            # still close intentional partials.
            try:
                v2 = order.order
                if v2 is not None:
                    from .scan_service import _matched_products_from_order as _mpfo
                    expected_cards = _mpfo(v2)
                    expected_n = len(expected_cards)
                    scanned_n = order.items.count()
                    if expected_n and scanned_n != expected_n:
                        incomplete_warning = (
                            f"⚠ Colis {order.bordereau_barcode} : {scanned_n} unité(s) "
                            f"scannée(s) mais {expected_n} attendue(s) selon la commande "
                            f"— vérifiez que l'ensemble est complet."
                        )
                        try:
                            from .models import log_action, AuditLog
                            log_action(
                                user, AuditLog.SCAN_SHIPPING,
                                description=(f"ALERTE ensemble incomplet : {order.bordereau_barcode} "
                                             f"— {scanned_n}/{expected_n} pièces"),
                                target_order_barcode=order.bordereau_barcode,
                            )
                        except Exception:
                            pass
            except Exception:
                pass
            order.status = ShippingOrder.CLOSED
            order.closed_at = timezone.now()
            order.save()
            for item in order.items.select_related("unit"):
                item.unit.status = ProductUnit.SHIPPED
                item.unit.save()
                item.status_at_close = ProductUnit.SHIPPED
                item.save(update_fields=["status_at_close"])
                StockMovement.objects.create(
                    unit=item.unit, movement_type=StockMovement.SHIPPED,
                    reference=order.bordereau_barcode, user=user,
                )
            closed_order = order

            # Customer SMS: order shipped (expédié). Fires once per v2 order.
            try:
                v2 = order.order  # linked v2 Order, if any
                if v2 is not None and not getattr(v2, "sms_expedie_sent", False):
                    from . import sms_service
                    total = sms_service._fmt_total(v2)
                    ok, _info = sms_service.send_sms(
                        v2.customer.phone if v2.customer else "",
                        sms_service.msg_expedie(total),
                    )
                    if ok:
                        v2.sms_expedie_sent = True
                        v2.save(update_fields=["sms_expedie_sent"])
            except Exception:
                pass

        # Check if this bordereau already exists
        existing = ShippingOrder.objects.filter(bordereau_barcode=barcode).first()
        if existing:
            if existing.status == ShippingOrder.OPEN:
                return {
                    "status": "ok", "type": "bordereau",
                    "message": f"Ordre {barcode} déjà ouvert.",
                    "new_order": {
                        "id": existing.id,
                        "bordereau_barcode": existing.bordereau_barcode,
                    },
                    "closed_order": {"id": closed_order.id, "bordereau_barcode": closed_order.bordereau_barcode} if closed_order else None,
                }
            else:
                return {"status": "error", "message": f"Ce bordereau ({barcode}) a déjà été utilisé.", "code": "BORDEREAU_DUPLICATE"}

        # Create order immediately — no Navex call here to avoid blocking
        new_order = ShippingOrder.objects.create(
            bordereau_barcode=barcode,
            status=ShippingOrder.OPEN,
        )

        # NEW: link to a v2 Order if one exists with this bordereau.
        # When an Order v2 is created and pushed to Navex, Navex returns a
        # bordereau that's stored on Order.bordereau_barcode. If the warehouse
        # team scans that same bordereau here, we link the v1 ShippingOrder
        # to the v2 Order so the units they scan are visible on both sides.
        try:
            from .models import Order as OrderV2
            v2_order = OrderV2.objects.filter(bordereau_barcode=barcode).first()
            if v2_order is not None:
                new_order.order = v2_order
                new_order.save(update_fields=["order"])
        except Exception:
            # Defensive — never break the scan flow even if the link lookup fails
            pass

    return {
        "status": "ok", "type": "bordereau",
        "message": f"Ordre {new_order.bordereau_barcode} ouvert.",
        "incomplete_warning": incomplete_warning,
        "new_order": {
            "id": new_order.id,
            "bordereau_barcode": new_order.bordereau_barcode,
        },
        "closed_order": {"id": closed_order.id, "bordereau_barcode": closed_order.bordereau_barcode} if closed_order else None,
    }


def _handle_unit_scan(barcode: str, user=None) -> dict:
    open_order = ShippingOrder.objects.filter(status=ShippingOrder.OPEN).first()
    if not open_order:
        return {"status": "error", "message": "Aucun ordre ouvert. Scannez d'abord un bordereau.", "code": "NO_OPEN_ORDER"}

    try:
        unit = ProductUnit.objects.select_related("variant__product").get(barcode=barcode)
    except ProductUnit.DoesNotExist:
        return {"status": "error", "message": f"Unité introuvable : {barcode}", "code": "UNIT_NOT_FOUND"}

    if OrderItem.objects.filter(order=open_order, unit=unit).exists():
        return {"status": "error", "message": f"{barcode} est déjà dans cet ordre.", "code": "ALREADY_IN_ORDER"}

    # in_stock and returned can both be shipped
    if unit.status not in (ProductUnit.IN_STOCK, ProductUnit.RETURNED):
        msgs = {
            ProductUnit.SHIPPED: "déjà expédié",
            ProductUnit.PAID:    "déjà payé",
        }
        return {"status": "error", "message": f"{barcode} ne peut pas être ajouté — {msgs.get(unit.status, unit.get_status_display())}.", "code": "INVALID_STATUS"}

    with transaction.atomic():
        OrderItem.objects.create(order=open_order, unit=unit, status_at_scan=ProductUnit.SHIPPED)
        unit.status = ProductUnit.SHIPPED
        unit.save()

    variant = unit.variant
    return {
        "status": "ok", "type": "unit",
        "message": f"{variant.product.name} {variant.color_label} — {unit.size} ajouté.",
        "unit": {
            "barcode": unit.barcode, "size": unit.size, "status": unit.status,
            "product_name": variant.product.name, "color_label": variant.color_label,
            "sell_price": str(variant.product.sell_price),
            "image_url": variant.image.url if variant.image else None,
        },
        "order": {"id": open_order.id, "bordereau_barcode": open_order.bordereau_barcode, "unit_count": open_order.items.count()},
    }


def handle_stock_scan(barcode: str, user=None) -> dict:
    """Add new stock — only creates new units, never modifies existing ones."""
    barcode = barcode.strip().upper()
    parsed = parse_barcode(barcode)

    if not parsed:
        return {"status": "error", "message": f"Format invalide : {barcode}. Attendu : CODE-COULEUR-TAILLE-NUM (ex: RLF-RED-40-001)", "code": "INVALID_FORMAT"}

    if ProductUnit.objects.filter(barcode=barcode).exists():
        return {"status": "error", "message": f"Ce barcode existe déjà en base.", "code": "DUPLICATE_BARCODE"}

    try:
        product = Product.objects.get(code=parsed.product_code)
    except Product.DoesNotExist:
        return {"status": "error", "message": f"Produit inconnu : {parsed.product_code}. Créez-le dans l'admin.", "code": "PRODUCT_NOT_FOUND"}

    try:
        variant = ProductVariant.objects.get(product=product, color_name=parsed.color_name)
    except ProductVariant.DoesNotExist:
        return {"status": "error", "message": f"Variante inconnue : {parsed.color_name} pour {parsed.product_code}.", "code": "VARIANT_NOT_FOUND"}

    with transaction.atomic():
        unit = ProductUnit.objects.create(variant=variant, barcode=barcode, size=parsed.size, status=ProductUnit.IN_STOCK)
        StockMovement.objects.create(unit=unit, movement_type=StockMovement.RECEIVED, reference="RECEPTION", user=user)

    return {
        "status": "ok", "type": "received",
        "message": f"{variant.product.name} {variant.color_label} taille {unit.size} ajouté au stock.",
        "unit": {
            "barcode": unit.barcode, "size": unit.size,
            "product_name": variant.product.name, "color_label": variant.color_label,
            "sell_price": str(variant.product.sell_price),
            "image_url": variant.image.url if variant.image else None,
        },
    }
