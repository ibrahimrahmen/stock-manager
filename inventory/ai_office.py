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
    AIEmployee, AITask, Expense, OrderItem, ProductUnit, ShippingOrder,
    StockMovement,
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


def create_finance_task(employee, date_from, date_to, label, instruction="",
                        user=None, triggered_by="owner"):
    return AITask.objects.create(
        employee=employee,
        title=f"Bénéfice / pertes — {label}",
        instruction=(instruction or "")[:2000],
        period_from=date_from, period_to=date_to,
        triggered_by=triggered_by, requested_by=user,
    )


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
    if task.instruction:
        parts.append("QUESTION DU PATRON : " + task.instruction)
    parts.append("Écris ton rapport maintenant (max ~200 mots).")
    return "\n\n".join(parts)


def run_task(task):
    """Execute a task synchronously. Never raises: errors land on the task."""
    from .views import _cfg, _claude_generate

    task.status = AITask.WORKING
    task.started_at = timezone.now()
    task.error = ""
    task.save(update_fields=["status", "started_at", "error"])
    try:
        if task.employee.role != AIEmployee.ROLE_FINANCE:
            raise ValueError(f"no worker for role {task.employee.role}")
        data = compute_finance(task.period_from, task.period_to)
        task.result_data = data
        task.save(update_fields=["result_data"])

        errbox = []
        model = (_cfg("AI_OFFICE_MODEL", "") or DEFAULT_MODEL).strip()
        text = _claude_generate(
            _finance_prompt(task, data), max_tokens=1200, temperature=None,
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
