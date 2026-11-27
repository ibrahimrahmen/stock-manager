"""AI office — the work done by the AI "employees".

Golden rule: every NUMBER is computed here, by code, from the database (and
Meta for ad spend). Claude only receives those finished numbers and explains
them to the owner in plain Tunisian Arabic. It never calculates or invents.

Public entry points:
    compute_finance(date_from, date_to)  -> dict of figures (JSON-safe)
    run_task(task)                        -> executes an AITask end to end
    create_finance_task(...)              -> builds a finance AITask for a period
"""
import json
from datetime import timedelta
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from .models import (
    Ad, AIConversation, AIEmployee, AITask, Expense, Order, OrderItem, ProductUnit,
    ShippingOrder, StockMovement,
)

# Same per-return cost the revenue page uses, so both pages agree.
RETURN_FEE = Decimal("1")
DEFAULT_MODEL = "claude-sonnet-5-5"


def _f(x):
    """Decimal/None -> float rounded to the millime, for JSON."""
    return round(float(x or 0), 3)


# ---------------------------------------------------------------------------
# Finance figures
# ---------------------------------------------------------------------------
def _ad_spend(date_from, date_to):
    """(total, available, top_campaigns). Best-effort: Meta can fail."""
    try:
        from .views import _meta_fetch_spend_by_campaign
        spend_map = _meta_fetch_spend_by_campaign(date_from, date_to) or {}
    except Exception:
        spend_map = {}
    if not spend_map:
        return Decimal("0"), False, []
    total = Decimal("0")
    rows = []
    for v in spend_map.values():
        s = Decimal(str(v.get("spend", 0) or 0))
        total += s
        rows.append({"name": v.get("name", "?"), "spend": _f(s)})
    rows.sort(key=lambda r: -r["spend"])
    return total, True, rows[:8]


def compute_finance(date_from, date_to, with_ads=True, with_previous=True):
    """Profit & loss for [date_from, date_to] (inclusive dates).

    Sales  = units marked PAID inside shipments paid (fully or partly) in the
             period, valued at the catalogue sell price; cost = buy price.
    Returns = return scans (StockMovement RETURNED) in the period, 1 TND each.
    Net profit = sales - purchase cost - return fees - ad spend - expenses.
    """
    paid_ships = ShippingOrder.objects.filter(
        status__in=ShippingOrder.PAID_STATUSES,
        paid_at__date__gte=date_from, paid_at__date__lte=date_to,
    )
    n_shipments = paid_ships.count()
    n_partial = paid_ships.filter(status=ShippingOrder.PARTIAL_PAID).count()
    cash_collected = paid_ships.aggregate(s=Sum("amount_collected"))["s"] or Decimal("0")

    products = {}

    def prod_row(p):
        r = products.get(p.id)
        if r is None:
            r = products[p.id] = {
                "name": p.name, "code": p.code, "sold": 0, "returned": 0,
                "sales": Decimal("0"), "cost": Decimal("0"),
            }
        return r

    total_sell = Decimal("0")
    total_buy = Decimal("0")
    units_sold = 0
    items = (OrderItem.objects.filter(order__in=paid_ships)
             .select_related("unit__variant__product"))
    for it in items:
        if it.display_status != ProductUnit.PAID:
            continue
        p = it.unit.variant.product
        r = prod_row(p)
        r["sold"] += 1
        r["sales"] += p.sell_price
        r["cost"] += p.buy_price
        total_sell += p.sell_price
        total_buy += p.buy_price
        units_sold += 1

    returns = (StockMovement.objects.filter(
        movement_type=StockMovement.RETURNED,
        moved_at__date__gte=date_from, moved_at__date__lte=date_to,
    ).select_related("unit__variant__product"))
    n_returns = 0
    for mv in returns:
        prod_row(mv.unit.variant.product)["returned"] += 1
        n_returns += 1
    return_fees = RETURN_FEE * n_returns

    if with_ads:
        ad_spend, ads_ok, top_campaigns = _ad_spend(date_from, date_to)
    else:
        ad_spend, ads_ok, top_campaigns = Decimal("0"), False, []

    exp_qs = Expense.objects.filter(date__gte=date_from, date__lte=date_to)
    total_expenses = exp_qs.aggregate(s=Sum("amount"))["s"] or Decimal("0")
    by_main = {}
    for row in exp_qs.values("category").annotate(t=Sum("amount")):
        main = Expense.SUB_TO_MAIN.get(row["category"], "divers")
        label = Expense.MAIN_LABELS.get(main, main)
        by_main[label] = by_main.get(label, Decimal("0")) + (row["t"] or 0)

    gross_margin = total_sell - total_buy
    net_profit = gross_margin - return_fees - ad_spend - total_expenses

    prod_list = []
    for r in products.values():
        handled = r["sold"] + r["returned"]
        prod_list.append({
            "name": r["name"], "code": r["code"],
            "sold": r["sold"], "returned": r["returned"],
            "return_rate_pct": round(100 * r["returned"] / handled, 1) if handled else 0,
            "sales": _f(r["sales"]), "margin": _f(r["sales"] - r["cost"]),
        })
    top_margin = sorted(prod_list, key=lambda r: -r["margin"])[:6]
    most_returned = sorted([r for r in prod_list if r["returned"]],
                           key=lambda r: -r["returned"])[:6]

    data = {
        "period": {"from": date_from.isoformat(), "to": date_to.isoformat(),
                   "days": (date_to - date_from).days + 1},
        "sales": {
            "paid_shipments": n_shipments,
            "partially_paid_shipments": n_partial,
            "units_sold": units_sold,
            "sales_catalog_price": _f(total_sell),
            "cash_collected_by_navex": _f(cash_collected),
            "purchase_cost": _f(total_buy),
            "gross_margin": _f(gross_margin),
            "margin_pct": round(float(gross_margin / total_sell * 100), 1) if total_sell else 0,
        },
        "returns": {"count": n_returns, "fees": _f(return_fees),
                    "fee_per_return": _f(RETURN_FEE)},
        "ads": {
            "available": ads_ok,
            "spend": _f(ad_spend),
            "spend_per_unit_sold": _f(ad_spend / units_sold) if (ads_ok and units_sold) else None,
            "top_campaigns": top_campaigns,
        },
        "expenses": {"total": _f(total_expenses),
                     "by_category": {k: _f(v) for k, v in
                                     sorted(by_main.items(), key=lambda kv: -kv[1])}},
        "net_profit": _f(net_profit),
        "products_top_margin": top_margin,
        "products_most_returned": most_returned,
    }

    if with_previous:
        length = date_to - date_from
        prev_to = date_from - timedelta(days=1)
        prev_from = prev_to - length
        prev = compute_finance(prev_from, prev_to, with_ads=with_ads, with_previous=False)
        data["previous_period"] = {
            "period": prev["period"],
            "net_profit": prev["net_profit"],
            "units_sold": prev["sales"]["units_sold"],
            "gross_margin": prev["sales"]["gross_margin"],
            "returns": prev["returns"]["count"],
            "ad_spend": prev["ads"]["spend"],
            "ads_available": prev["ads"]["available"],
            "expenses": prev["expenses"]["total"],
        }
    return data


# ---------------------------------------------------------------------------
# Marketing figures: real profit per Meta campaign
# ---------------------------------------------------------------------------
# Order status buckets (v2 Order) for ad performance.
NOT_SHIPPED = ("non_confirmee", "injoignable", "pas_serieux", "rappeler_plus_tard",
               "annulee", "supprime_navex")
PENDING = ("confirmee", "en_cours", "au_magasin")
DELIVERED = ("livree", "payee")
RETURNED = ("returning", "returned")

# Verdict rules (computed by code, so the AI never decides on a whim).
MIN_SPEND_TO_JUDGE = Decimal("30")    # below this, too early to judge
MIN_ORDERS_TO_SCALE = 5
STOP_RETURN_RATE = 50.0               # % of finished orders returned
SCALE_MAX_RETURN_RATE = 30.0
MIN_FINISHED_FOR_RATE = 4             # finished orders needed to trust a return rate


def _verdict(c):
    """scale / watch / stop / untracked + the reason, from the campaign figures."""
    spend = Decimal(str(c["spend"]))
    if c["leads"] == 0 and not c["tracked"]:
        # We simply cannot see this campaign's orders (e.g. Converty orders carry
        # no campaign name and the ad has no offer link). Never call it a loser.
        return "untracked", ("aucune commande rattachée : liez cette pub à son "
                             "offre dans « Ads & offres » pour la juger")
    rr = c["return_rate_pct"]
    rr_known = c["finished"] >= MIN_FINISHED_FOR_RATE
    if spend < MIN_SPEND_TO_JUDGE and c["confirmed"] < 3:
        return "watch", "trop tôt pour juger (peu de dépense et de commandes)"
    if rr_known and rr >= STOP_RETURN_RATE:
        return "stop", f"{rr:.0f}% des commandes terminées reviennent en retour"
    if c["est_profit"] < 0 and spend >= MIN_SPEND_TO_JUDGE:
        return "stop", "la pub coûte plus que la marge qu'elle rapporte"
    if (c["est_profit"] > 0 and c["confirmed"] >= MIN_ORDERS_TO_SCALE
            and (not rr_known or rr < SCALE_MAX_RETURN_RATE)):
        return "scale", "rentable avec assez de commandes et peu de retours"
    return "watch", "résultat moyen ou pas encore assez clair"


def _in_stock_by_product(product_ids):
    from django.db.models import Count
    rows = (ProductUnit.objects.filter(variant__product_id__in=product_ids,
                                       status=ProductUnit.IN_STOCK)
            .values("variant__product_id").annotate(n=Count("id")))
    return {r["variant__product_id"]: r["n"] for r in rows}


def compute_marketing(date_from, date_to, with_previous=True):
    """Per-campaign performance for orders CREATED in [date_from, date_to].

    Each order is attributed to ONE campaign: the campaign the customer came
    from (captured on the Messenger/Instagram conversation), else the ad linked
    to an offer in the order (same rule as the Ads & offres page), else Barats.tn
    carousel ads for orders on the Barats.tn page, else "organic".
    est_profit = (net sales − purchase cost) of shipped orders not returned
                 − ad spend − 1 TND per return.
    Orders still on the way may yet be returned: the return rate is measured
    only on finished orders (delivered or returned).
    """
    try:
        from .views import _meta_fetch_spend_by_campaign
        spend_map = _meta_fetch_spend_by_campaign(date_from, date_to) or {}
        ads_ok = bool(spend_map)
    except Exception:
        spend_map, ads_ok = {}, False

    ads = list(Ad.objects.prefetch_related("links__offer__products").all())
    ad_by_cid = {a.campaign_id: a for a in ads if a.campaign_id}

    camps = {}   # key -> row

    def camp(key, name, status=""):
        c = camps.get(key)
        if c is None:
            c = camps[key] = {
                "name": name, "status": status, "spend": Decimal("0"),
                "leads": 0, "not_shipped": 0, "pending": 0, "delivered": 0,
                "returned": 0, "net_sales": Decimal("0"), "cost": Decimal("0"),
                "product_ids": set(),
            }
        return c

    barats_key = "barats_carousel"
    for cid, v in spend_map.items():
        a = ad_by_cid.get(cid)
        if a is not None and a.attribution == Ad.ATTR_BARATS:
            c = camp(barats_key, "Barats.tn (carrousel, toutes pubs)")
        else:
            c = camp(cid, v.get("name") or (a.campaign_name if a else cid),
                     a.effective_status if a else "")
        c["spend"] += Decimal(str(v.get("spend", 0) or 0))

    # Attribution maps.
    name_to_key, adid_to_key = {}, {}
    offer_page_to_key, offer_any_to_key = {}, {}
    for a in ads:
        key = barats_key if a.attribution == Ad.ATTR_BARATS else (a.campaign_id or f"ad{a.id}")
        adid_to_key[a.id] = key
        if a.campaign_name:
            name_to_key.setdefault(a.campaign_name.strip().lower(), key)
        if a.attribution == Ad.ATTR_BARATS:
            continue
        for lk in a.links.all():
            if lk.sales_page_id:
                offer_page_to_key.setdefault((lk.offer_id, lk.sales_page_id), key)
            else:
                offer_any_to_key.setdefault(lk.offer_id, key)
    for cid, v in spend_map.items():
        if v.get("name"):
            name_to_key.setdefault(v["name"].strip().lower(),
                                   adid_to_key.get(getattr(ad_by_cid.get(cid), "id", None), cid))
    key_names = {adid_to_key[a.id]: a.campaign_name for a in ads}
    key_status = {adid_to_key[a.id]: a.effective_status for a in ads}
    key_products = {}
    for a in ads:
        ps = key_products.setdefault(adid_to_key[a.id], set())
        for lk in a.links.all():
            ps.update(op.product_id for op in lk.offer.products.all())

    orders = (Order.objects
              .filter(created_at__date__gte=date_from, created_at__date__lte=date_to,
                      exchange_of__isnull=True)
              .select_related("sales_page")
              .prefetch_related("order_offers", "lines__product",
                                "messenger_conversations"))
    organic = camp("organic", "Sans pub (organique / non attribué)")
    for o in orders:
        key = None
        for cv in o.messenger_conversations.all():
            for nm in (cv.source_campaign_name, cv.source_campaign):
                nm = (nm or "").strip().lower()
                if nm and nm in name_to_key:
                    key = name_to_key[nm]
                    break
            if key is None and cv.matched_ad_id in adid_to_key:
                key = adid_to_key[cv.matched_ad_id]
            if key:
                break
        if key is None:
            for oo in o.order_offers.all():
                key = (offer_page_to_key.get((oo.offer_id, o.sales_page_id))
                       or offer_any_to_key.get(oo.offer_id))
                if key:
                    break
        if key is None and o.sales_page and o.sales_page.name.strip().lower() == "barats.tn" \
                and barats_key in camps:
            key = barats_key
        c = camps.get(key) if key else organic
        if c is None:   # attributed to an ad with no spend in the period
            c = camp(key, key_names.get(key, key), key_status.get(key, ""))
        c["leads"] += 1
        if o.status in NOT_SHIPPED:
            c["not_shipped"] += 1
            continue
        if o.status in RETURNED:
            c["returned"] += 1
            continue
        c["delivered" if o.status in DELIVERED else "pending"] += 1
        c["net_sales"] += (o.total or Decimal("0")) - (o.delivery_fee or Decimal("0"))
        c["cost"] += sum((ln.product.buy_price * ln.quantity for ln in o.lines.all()),
                         Decimal("0"))

    all_pids = set().union(*key_products.values()) if key_products else set()
    stock = _in_stock_by_product(all_pids) if all_pids else {}
    from .models import Product
    prod_info = {p.id: p for p in Product.objects.filter(id__in=all_pids)}

    rows = []
    for key, c in camps.items():
        if c["spend"] <= 0 and c["leads"] == 0:
            continue
        confirmed = c["pending"] + c["delivered"] + c["returned"]
        finished = c["delivered"] + c["returned"]
        margin = c["net_sales"] - c["cost"]
        est_profit = margin - c["spend"] - RETURN_FEE * c["returned"]
        low_stock = []
        for pid in sorted(key_products.get(key, ())):
            p = prod_info.get(pid)
            n = stock.get(pid, 0)
            if p and not p.archived and n <= max(p.alert_threshold, 3):
                low_stock.append({"product": p.name, "in_stock": n})
        row = {
            "name": c["name"], "meta_status": c["status"] or "",
            # Can we see this campaign's orders at all? Yes if it is the Barats
            # carousel pool, or the ad is linked to an offer, or it already got
            # at least one order through the captured conversation campaign.
            "tracked": key == barats_key or bool(key_products.get(key)) or c["leads"] > 0,
            "spend": _f(c["spend"]), "leads": c["leads"],
            "not_shipped": c["not_shipped"], "confirmed": confirmed,
            "pending": c["pending"], "delivered": c["delivered"],
            "returned": c["returned"], "finished": finished,
            "return_rate_pct": round(100 * c["returned"] / finished, 1) if finished else 0,
            "confirm_rate_pct": round(100 * confirmed / c["leads"], 1) if c["leads"] else 0,
            "cost_per_confirmed": _f(c["spend"] / confirmed) if confirmed else None,
            "net_sales": _f(c["net_sales"]), "margin": _f(margin),
            "est_profit": _f(est_profit), "low_stock_products": low_stock[:5],
            "is_organic": key == "organic",
        }
        if key == "organic":
            row["verdict"], row["reason"] = None, ""
        else:
            row["verdict"], row["reason"] = _verdict(row)
        rows.append(row)
    rows.sort(key=lambda r: (r["is_organic"], -r["spend"]))

    paid = [r for r in rows if not r["is_organic"]]
    tot_spend = sum(r["spend"] for r in paid)
    tot_conf = sum(r["confirmed"] for r in paid)
    data = {
        "period": {"from": date_from.isoformat(), "to": date_to.isoformat(),
                   "days": (date_to - date_from).days + 1},
        "ads_available": ads_ok,
        "totals": {
            "ad_spend": round(tot_spend, 3),
            "ad_orders_confirmed": tot_conf,
            "cost_per_confirmed": round(tot_spend / tot_conf, 3) if tot_conf else None,
            "ad_est_profit": round(sum(r["est_profit"] for r in paid), 3),
            "campaigns": len(paid),
            "scale": sum(1 for r in paid if r["verdict"] == "scale"),
            "watch": sum(1 for r in paid if r["verdict"] == "watch"),
            "stop": sum(1 for r in paid if r["verdict"] == "stop"),
            "untracked": sum(1 for r in paid if r["verdict"] == "untracked"),
            "untracked_spend": round(sum(r["spend"] for r in paid
                                         if r["verdict"] == "untracked"), 3),
        },
        "rules": {
            "min_spend_to_judge": _f(MIN_SPEND_TO_JUDGE),
            "stop_if_return_rate_pct_at_least": STOP_RETURN_RATE,
            "scale_needs_orders": MIN_ORDERS_TO_SCALE,
            "scale_max_return_rate_pct": SCALE_MAX_RETURN_RATE,
        },
        "campaigns": rows[:25],
    }
    if with_previous:
        length = date_to - date_from
        prev_to = date_from - timedelta(days=1)
        prev = compute_marketing(prev_to - length, prev_to, with_previous=False)
        data["previous_period"] = {"period": prev["period"], **prev["totals"]}
    return data


# ---------------------------------------------------------------------------
# Running tasks
# ---------------------------------------------------------------------------
def period_for(key, today=None):
    """Named periods used by the office buttons and the daily cron."""
    today = today or timezone.localdate()
    if key == "yesterday":
        d = today - timedelta(days=1)
        return d, d, "Hier"
    if key == "last7":
        return today - timedelta(days=7), today - timedelta(days=1), "7 derniers jours"
    if key == "month":
        return today.replace(day=1), today, "Ce mois-ci"
    if key == "last_month":
        last = today.replace(day=1) - timedelta(days=1)
        return last.replace(day=1), last, "Le mois dernier"
    raise ValueError(f"unknown period {key!r}")


ROLE_TASK_TITLES = {
    AIEmployee.ROLE_FINANCE: "Bénéfice / pertes",
    AIEmployee.ROLE_MARKETING: "Pubs : garder / surveiller / arrêter",
}
# The period each employee reports on in the automatic morning run.
ROLE_DAILY_PERIOD = {
    AIEmployee.ROLE_FINANCE: "yesterday",
    AIEmployee.ROLE_MARKETING: "last7",   # one day is too little to judge an ad
}


def create_task(employee, date_from, date_to, label, instruction="",
                user=None, triggered_by="owner"):
    title = ROLE_TASK_TITLES.get(employee.role, "Tâche")
    return AITask.objects.create(
        employee=employee,
        title=f"{title} — {label}",
        instruction=(instruction or "")[:2000],
        period_from=date_from, period_to=date_to,
        triggered_by=triggered_by, requested_by=user,
    )


# Back-compat name used by the first version.
create_finance_task = create_task


def _common_prompt_tail(task):
    parts = []
    if task.instruction:
        parts.append("QUESTION DU PATRON : " + task.instruction)
    parts.append(
        f"La période analysée s'appelle « {task.title} » "
        f"(du {task.period_from} au {task.period_to}) ; nous sommes le "
        f"{timezone.localdate()}. Nomme-la correctement (ex. البارح pour hier, "
        "ce n'est pas اليوم).")
    parts.append(
        "Langue : darija TUNISIENNE uniquement (برشة، شنوة، علاش، البارح، اليوم، "
        "توة). Jamais de mots égyptiens (النهارده، إمبارح، دلوقتي، كده).")
    return parts


def _finance_prompt(task, data):
    parts = [
        "Voici les chiffres calculés par le logiciel pour la période demandée "
        "(montants en dinars tunisiens, TND). Ce sont les seuls chiffres "
        "que tu as le droit d'utiliser.",
        "Définitions : sales_catalog_price = ventes au prix catalogue ; "
        "cash_collected_by_navex = argent réellement encaissé par Navex (inclut "
        "les frais de livraison payés par le client et les réductions) ; "
        "net_profit = marge brute − frais de retour − publicité − dépenses.",
        "CHIFFRES (JSON):",
        json.dumps(data, ensure_ascii=False, indent=1),
    ]
    parts += _common_prompt_tail(task)
    parts.append("Écris ton rapport maintenant (max ~200 mots) et termine-le "
                 "complètement, avec les actions de la semaine.")
    return "\n\n".join(parts)


def _marketing_prompt(task, data):
    parts = [
        "Voici la performance de chaque campagne Meta, calculée par le logiciel "
        "(montants en TND). Ce sont les seuls chiffres que tu as le droit d'utiliser.",
        "Définitions : leads = commandes reçues ; confirmed = commandes expédiées "
        "(en route + livrées + retournées) ; not_shipped = annulées / injoignables / "
        "pas sérieux ; return_rate_pct = retours / commandes terminées (livrées + "
        "retournées) ; est_profit = (ventes nettes − prix d'achat) des commandes "
        "non retournées − pub − 1 DT par retour. Les commandes 'pending' peuvent "
        "encore revenir en retour. verdict = décision calculée par des règles fixes "
        "(scale = augmenter, watch = surveiller, stop = arrêter, untracked = on ne "
        "voit pas ses commandes) avec sa raison : explique-la, ne la change pas. "
        "Une campagne 'untracked' n'est PAS une mauvaise pub : dis au patron de la "
        "lier à son offre dans la page « Ads & offres », ne recommande jamais de "
        "l'arrêter.",
        "CHIFFRES (JSON):",
        json.dumps(data, ensure_ascii=False, indent=1),
    ]
    parts += _common_prompt_tail(task)
    parts.append("Écris ton rapport maintenant (max ~250 mots) et termine-le "
                 "complètement. Tu RECOMMANDES seulement : le patron décide et "
                 "agit lui-même dans Ads Manager.")
    return "\n\n".join(parts)


WORKERS = {
    AIEmployee.ROLE_FINANCE: (compute_finance, _finance_prompt),
    AIEmployee.ROLE_MARKETING: (compute_marketing, _marketing_prompt),
}


def run_task(task):
    """Execute a task synchronously. Never raises: errors land on the task."""
    from .views import _cfg, _claude_generate

    task.status = AITask.WORKING
    task.started_at = timezone.now()
    task.error = ""
    task.save(update_fields=["status", "started_at", "error"])
    try:
        worker = WORKERS.get(task.employee.role)
        if worker is None:
            raise ValueError(f"no worker for role {task.employee.role}")
        compute, build_prompt = worker
        data = compute(task.period_from, task.period_to)
        task.result_data = data
        task.save(update_fields=["result_data"])

        errbox = []
        model = (_cfg("AI_OFFICE_MODEL", "") or DEFAULT_MODEL).strip()
        text = _claude_generate(
            build_prompt(task, data), max_tokens=3000, temperature=None,
            system=task.employee.job_description or None, model=model,
            errbox=errbox, timeout=60,
        )
        if text:
            task.result_text = text.strip()
        else:
            task.error = ("Les chiffres sont prêts, mais l'explication IA a échoué. "
                          + "; ".join(errbox))[:2000]
        task.status = AITask.DONE
    except Exception as exc:  # keep the office alive whatever happens
        task.status = AITask.FAILED
        task.error = f"{type(exc).__name__}: {exc}"[:2000]
    task.finished_at = timezone.now()
    task.save()
    return task


# ---------------------------------------------------------------------------
# Conversation (chat) with an employee
# ---------------------------------------------------------------------------
CHAT_MAX_TURNS = 20          # messages of history sent to Claude
CHAT_MAX_CHARS = 2000        # per owner message


def _claude_chat(system, messages, model, max_tokens=4000, timeout=60, errbox=None):
    """Multi-turn Claude call (the shared helper only sends one user turn).
    messages = [{"role": "user"|"assistant", "content": str}]. Returns text or None."""
    import urllib.request
    from .views import _cfg
    api_key = _cfg("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        if errbox is not None:
            errbox.append("ANTHROPIC_API_KEY manquant")
        return None
    body = {"model": model, "max_tokens": max_tokens, "messages": messages,
            "system": [{"type": "text", "text": system,
                        "cache_control": {"type": "ephemeral"}}]}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages",
                                 data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("x-api-key", api_key)
    req.add_header("anthropic-version", "2023-06-01")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            rd = json.loads(resp.read().decode("utf-8"))
        text = "".join(b.get("text", "") for b in rd.get("content") or []
                       if b.get("type") == "text").strip()
        # max_tokens also covers the model's own reasoning on newer models, so
        # an answer can be cut: say so instead of ending mid-word silently.
        if text and rd.get("stop_reason") == "max_tokens":
            text += " …"
        return text or None
    except Exception as e:
        msg = str(e)
        try:
            if hasattr(e, "read"):
                msg += " | " + e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        if errbox is not None:
            errbox.append(msg[:400])
        return None


def _chat_system(employee):
    last = employee.tasks.filter(status=AITask.DONE).exclude(result_data={}).first()
    parts = [employee.job_description or "",
             "\n--- MODE DISCUSSION ---",
             "Le patron te parle directement, comme à un collègue. Tes réponses "
             "seront LUES À VOIX HAUTE par une voix anglaise automatique.",
             "LANGUE EN MODE DISCUSSION : réponds TOUJOURS en anglais simple et "
             "parlé, même si le patron écrit en arabe, en darija ou en français. "
             "Cette règle remplace la consigne de langue de ta fiche de poste. "
             "Comprends bien la darija tunisienne du patron (la reconnaissance "
             "vocale peut déformer quelques mots : devine le sens). Quand le "
             f"patron commence par ton nom (« {employee.name} … », ou son écriture "
             "arabe) ou dit « إجا لهنا », il s'adresse simplement à toi : ne parle "
             "pas de toi à la troisième personne.",
             "STYLE : parle comme un vrai collègue humain au téléphone, pas comme "
             "un robot qui lit un rapport. Naturel et chaleureux, contractions "
             "anglaises (it's, we're, don't), petites réactions naturelles (Yeah, "
             "Honestly, Good question, Hmm), phrases courtes. 1 à 4 phrases, jamais "
             "de liste. Pas plus de 2 ou 3 chiffres par réponse : choisis les plus "
             "importants. Si c'est utile, termine par une courte question pour "
             "continuer la conversation. Pas de symboles, pas de markdown.",
             "Montants arrondis en dinars (ex. 691 dinars, pas 691.141), "
             "pourcentages arrondis. Garde les noms de campagnes tels quels. Le "
             "patron peut te couper la parole : s'il change de sujet, suis-le.",
             f"Nous sommes le {timezone.localdate()}."]
    if last:
        parts += [
            f"Ton dernier rapport : « {last.title} » (du {last.period_from} au "
            f"{last.period_to}). Chiffres calculés par le logiciel (les seuls que "
            "tu peux citer) :",
            json.dumps(last.result_data, ensure_ascii=False),
            "Ce que tu as écrit au patron :", last.result_text or "(pas de texte)",
            "Si le patron demande une autre période, dis-lui de te donner la tâche "
            "dans l'onglet « Tâche » : tu n'as pas ces chiffres-là.",
        ]
    else:
        parts.append("Tu n'as encore fait aucun rapport : propose au patron de te "
                     "donner une tâche dans l'onglet « Tâche » pour avoir des chiffres.")
    return "\n".join(parts)


def chat(employee, conversation, text):
    """Add the owner's message, get the employee's reply, save both.
    Returns (reply_text or None, error)."""
    from .views import _cfg
    text = (text or "").strip()[:CHAT_MAX_CHARS]
    now = timezone.now().isoformat()
    msgs = list(conversation.messages or [])
    msgs.append({"role": "user", "text": text, "at": now})
    history = [{"role": m["role"], "content": m["text"]}
               for m in msgs[-CHAT_MAX_TURNS:]]
    if history[0]["role"] != "user":       # the API needs to start with a user turn
        history = history[1:]
    errbox = []
    model = (_cfg("AI_OFFICE_MODEL", "") or DEFAULT_MODEL).strip()
    reply = _claude_chat(_chat_system(employee), history, model, errbox=errbox)
    if reply:
        msgs.append({"role": "assistant", "text": reply, "at": timezone.now().isoformat()})
    conversation.messages = msgs
    conversation.save()
    return reply, ("; ".join(errbox) if not reply else "")
