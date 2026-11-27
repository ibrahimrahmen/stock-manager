"""Tests for the AI office: finance figures, task running, API and daily cron."""
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from . import ai_office
from .models import (
    AIEmployee, AITask, Expense, OrderItem, Product, ProductUnit,
    ProductVariant, ShippingOrder, StockMovement,
)

DAY = date(2026, 11, 20)
NO_ADS = (Decimal("0"), False, [])
ADS_50 = (Decimal("50"), True, [{"name": "Camp A", "spend": 50.0}])


def _at(d, hour=12):
    return timezone.make_aware(datetime(d.year, d.month, d.day, hour))


class FinanceFiguresTests(TestCase):
    def setUp(self):
        self.p = Product.objects.create(name="Pull", code="PUL", buy_price=Decimal("20"),
                                        sell_price=Decimal("60"))
        self.v = ProductVariant.objects.create(product=self.p, color_name="BLK", color_label="Noir")
        self.n = 0

    def unit(self, status=ProductUnit.PAID):
        self.n += 1
        return ProductUnit.objects.create(variant=self.v, barcode=f"PUL-{self.n}", size="M", status=status)

    def paid_shipment(self, d, n_paid, n_returned=0, status=ShippingOrder.PAID, collected="127"):
        so = ShippingOrder.objects.create(bordereau_barcode=f"B{self.n}-{d}-{status}", status=status,
                                          paid_at=_at(d), amount_collected=Decimal(collected))
        for _ in range(n_paid):
            OrderItem.objects.create(order=so, unit=self.unit(), status_at_payment=ProductUnit.PAID)
        for _ in range(n_returned):
            OrderItem.objects.create(order=so, unit=self.unit(ProductUnit.RETURNED),
                                     status_at_payment=ProductUnit.RETURNED)
        return so

    def test_profit_formula(self):
        self.paid_shipment(DAY, 2, n_returned=1)                       # 2 sold
        self.paid_shipment(DAY, 1, status=ShippingOrder.PARTIAL_PAID, collected="67")  # partial counts
        self.paid_shipment(DAY - timedelta(days=3), 5)                 # outside period
        mv = StockMovement.objects.create(unit=self.unit(ProductUnit.RETURNED),
                                          movement_type=StockMovement.RETURNED)
        StockMovement.objects.filter(pk=mv.pk).update(moved_at=_at(DAY))
        Expense.objects.create(amount=Decimal("10"), category="loyer_pro", date=DAY)
        Expense.objects.create(amount=Decimal("99"), category="loyer_pro", date=DAY + timedelta(days=1))

        with patch.object(ai_office, "_ad_spend", return_value=ADS_50):
            d = ai_office.compute_finance(DAY, DAY)

        s = d["sales"]
        self.assertEqual(s["units_sold"], 3)
        self.assertEqual(s["paid_shipments"], 2)
        self.assertEqual(s["partially_paid_shipments"], 1)
        self.assertEqual(s["sales_catalog_price"], 180.0)
        self.assertEqual(s["purchase_cost"], 60.0)
        self.assertEqual(s["gross_margin"], 120.0)
        self.assertEqual(s["cash_collected_by_navex"], 194.0)
        self.assertEqual(d["returns"]["count"], 1)
        self.assertEqual(d["expenses"]["total"], 10.0)
        self.assertEqual(d["ads"]["spend_per_unit_sold"], round(50 / 3, 3))
        # 120 margin - 1 return fee - 50 ads - 10 expenses
        self.assertEqual(d["net_profit"], 59.0)
        self.assertEqual(d["products_most_returned"][0]["code"], "PUL")
        # previous period = the day before: nothing happened
        self.assertEqual(d["previous_period"]["net_profit"], -50.0)  # only the (mocked) ad spend
        json.dumps(d)  # must be JSON-safe for the JSONField

    def test_ads_unavailable_is_reported_not_guessed(self):
        self.paid_shipment(DAY, 1)
        with patch.object(ai_office, "_ad_spend", return_value=NO_ADS):
            d = ai_office.compute_finance(DAY, DAY, with_previous=False)
        self.assertFalse(d["ads"]["available"])
        self.assertIsNone(d["ads"]["spend_per_unit_sold"])
        self.assertEqual(d["net_profit"], 40.0)


class RunTaskTests(TestCase):
    def setUp(self):
        self.emp = AIEmployee.objects.get(slug="finance")   # hired by the migration

    def test_seeded_employee(self):
        self.assertEqual(self.emp.role, AIEmployee.ROLE_FINANCE)
        self.assertIn("JAMAIS", self.emp.job_description)

    @patch.object(ai_office, "_ad_spend", return_value=NO_ADS)
    def test_success(self, _ads):
        t = ai_office.create_finance_task(self.emp, DAY, DAY, "Hier", instruction="pourquoi ?")
        with patch("inventory.views._claude_generate", return_value="  الربح مليح  ") as gen:
            ai_office.run_task(t)
        t.refresh_from_db()
        self.assertEqual(t.status, AITask.DONE)
        self.assertEqual(t.result_text, "الربح مليح")
        self.assertIn("net_profit", t.result_data)
        kwargs = gen.call_args.kwargs
        self.assertEqual(kwargs["system"], self.emp.job_description)
        self.assertIn("pourquoi ?", gen.call_args.args[0])

    @patch.object(ai_office, "_ad_spend", return_value=NO_ADS)
    def test_ai_failure_keeps_numbers(self, _ads):
        t = ai_office.create_finance_task(self.emp, DAY, DAY, "Hier")
        with patch("inventory.views._claude_generate", return_value=None):
            ai_office.run_task(t)
        t.refresh_from_db()
        self.assertEqual(t.status, AITask.DONE)
        self.assertIn("net_profit", t.result_data)
        self.assertIn("échoué", t.error)

    def test_crash_marks_failed(self):
        t = ai_office.create_finance_task(self.emp, DAY, DAY, "Hier")
        with patch.object(ai_office, "compute_finance", side_effect=RuntimeError("boom")):
            ai_office.run_task(t)
        t.refresh_from_db()
        self.assertEqual(t.status, AITask.FAILED)
        self.assertIn("boom", t.error)

    def test_periods(self):
        today = date(2026, 11, 27)
        self.assertEqual(ai_office.period_for("yesterday", today)[:2], (date(2026, 11, 26),) * 2)
        self.assertEqual(ai_office.period_for("month", today)[:2], (date(2026, 11, 1), today))
        self.assertEqual(ai_office.period_for("last_month", today)[:2], (date(2026, 10, 1), date(2026, 10, 31)))


@patch.object(ai_office, "_ad_spend", return_value=NO_ADS)
@patch("inventory.views._claude_generate", return_value="rapport")
class OfficeApiTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser("owner", "o@example.com", "pw")
        self.staff = User.objects.create_user("staff", "s@example.com", "pw", is_staff=True)
        for u in (self.owner, self.staff):   # skip the forced first-login password change
            prof = getattr(u, "profile", None)
            if prof is not None and prof.must_change_password:
                prof.must_change_password = False
                prof.save()

    def test_owner_only(self, *_):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get("/ai-office/").status_code, 403)
        self.assertEqual(self.client.post("/ai-office/api/employees/finance/task/",
                                          "{}", content_type="application/json").status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get("/ai-office/api/state/").status_code, 302)

    def test_page_and_state(self, *_):
        self.client.force_login(self.owner)
        r = self.client.get("/ai-office/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "three.module.js")
        st = self.client.get("/ai-office/api/state/").json()
        self.assertEqual(st["employees"][0]["slug"], "finance")
        self.assertEqual(st["employees"][0]["state"], "idle")

    def test_give_task(self, *_):
        self.client.force_login(self.owner)
        r = self.client.post("/ai-office/api/employees/finance/task/",
                             json.dumps({"period": "last7", "instruction": "q"}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "done")
        self.assertEqual(r.json()["result_text"], "rapport")
        bad = self.client.post("/ai-office/api/employees/finance/task/",
                               json.dumps({"period": "forever"}), content_type="application/json")
        self.assertEqual(bad.status_code, 400)

    def test_busy_employee_refuses_second_task(self, *_):
        emp = AIEmployee.objects.get(slug="finance")
        AITask.objects.create(employee=emp, title="x", status=AITask.WORKING)
        self.client.force_login(self.owner)
        r = self.client.post("/ai-office/api/employees/finance/task/", "{}",
                             content_type="application/json")
        self.assertEqual(r.status_code, 409)

    def test_stale_working_task_expires(self, *_):
        emp = AIEmployee.objects.get(slug="finance")
        t = AITask.objects.create(employee=emp, title="x", status=AITask.WORKING)
        AITask.objects.filter(pk=t.pk).update(created_at=timezone.now() - timedelta(minutes=10))
        self.client.force_login(self.owner)
        self.client.get("/ai-office/api/state/")
        t.refresh_from_db()
        self.assertEqual(t.status, AITask.FAILED)

    def test_update_employee(self, *_):
        self.client.force_login(self.owner)
        r = self.client.post("/ai-office/api/employees/finance/update/",
                             json.dumps({"name": "Amine", "color": "#ff0000", "job_description": "jd"}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 200)
        e = AIEmployee.objects.get(slug="finance")
        self.assertEqual((e.name, e.color, e.job_description), ("Amine", "#ff0000", "jd"))
        r = self.client.post("/ai-office/api/employees/finance/update/", json.dumps({"name": " "}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_cron_one_employee_per_call_once_per_day(self, gen, _ads):
        with patch("inventory.views._meta_fetch_spend_by_campaign", return_value={}):
            r1 = self.client.get("/cron/ai-office-daily/").json()
            r2 = self.client.get("/cron/ai-office-daily/").json()
            r3 = self.client.get("/cron/ai-office-daily/").json()
        self.assertEqual({r1["employee"], r2["employee"]}, {"finance", "marketing"})
        self.assertEqual(r3["status"], "skipped")
        self.assertEqual(gen.call_count, 2)
        mk = AITask.objects.get(triggered_by="cron", employee__slug="marketing")
        self.assertEqual((mk.period_to - mk.period_from).days, 6)   # last 7 days


class ClaudeTemperatureTests(TestCase):
    """The newest Claude models reject `temperature`; None must omit it."""

    def _sent_body(self, **kw):
        sent = {}

        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"content":[{"type":"text","text":"ok"}]}'

        def fake_urlopen(req, timeout=None):
            sent["body"] = json.loads(req.data.decode("utf-8"))
            sent["timeout"] = timeout
            return _Resp()

        from . import views
        with patch.object(views, "_cfg", side_effect=lambda k, d="": "test-key" if k == "ANTHROPIC_API_KEY" else d), \
                patch("urllib.request.urlopen", fake_urlopen):
            self.assertEqual(views._claude_generate("hi", **kw), "ok")
        return sent

    def test_none_omits_temperature(self):
        sent = self._sent_body(temperature=None, timeout=60)
        self.assertNotIn("temperature", sent["body"])
        self.assertEqual(sent["timeout"], 60)

    def test_existing_callers_unchanged(self):
        sent = self._sent_body()
        self.assertEqual(sent["body"]["temperature"], 0.0)
        self.assertEqual(sent["timeout"], 15)



from .models import Ad, AdOfferLink, Customer, MessengerConversation, Offer, OfferProduct, Order, OrderLine, OrderOffer, SalesPage


class MarketingFiguresTests(TestCase):
    """Per-campaign real profit + verdict rules."""

    def setUp(self):
        self.p = Product.objects.create(name="Doudoune", code="DOU", buy_price=Decimal("40"),
                                        sell_price=Decimal("100"))
        self.v = ProductVariant.objects.create(product=self.p, color_name="BLK", color_label="Noir")
        self.cust = Customer.objects.create(phone="20000000", name="Test")
        self.page = SalesPage.objects.create(name="Arrow")
        self.offer = Offer.objects.create(name="Doudoune offre")
        OfferProduct.objects.create(offer=self.offer, product=self.p)
        self.good = Ad.objects.create(campaign_name="Good camp", campaign_id="C1", effective_status="ACTIVE")
        self.bad = Ad.objects.create(campaign_name="Bad camp", campaign_id="C2", effective_status="ACTIVE")
        AdOfferLink.objects.create(ad=self.good, offer=self.offer, sales_page=self.page)

    def order(self, status, campaign=None, total="107"):
        o = Order.objects.create(customer=self.cust, sales_page=self.page, status=status,
                                 total=Decimal(total), delivery_fee=Decimal("7"))
        Order.objects.filter(pk=o.pk).update(created_at=_at(DAY))
        oo = OrderOffer.objects.create(order=o, offer=self.offer, offer_name="Doudoune offre",
                                       bundle_price=Decimal("100"))
        OrderLine.objects.create(order=o, product=self.p, variant=self.v, size="M",
                                 quantity=1, unit_price=Decimal("100"), order_offer=oo)
        if campaign:
            MessengerConversation.objects.create(source_campaign_name=campaign, pending_order=o)
        return o

    def compute(self, spend):
        m = {"C1": {"name": "Good camp", "spend": spend.get("C1", 0)},
             "C2": {"name": "Bad camp", "spend": spend.get("C2", 0)}}
        with patch("inventory.views._meta_fetch_spend_by_campaign", return_value=m):
            return ai_office.compute_marketing(DAY, DAY, with_previous=False)

    def test_attribution_profit_and_verdicts(self):
        for _ in range(5):
            self.order("livree", "Good camp")       # direct (conversation) attribution
        self.order("en_cours")                       # via the offer link -> Good camp
        self.order("annulee", "Good camp")
        for _ in range(4):
            self.order("returned", "Bad camp")
        self.order("livree", "Bad camp")
        self.order("livree")                         # no link: but offer link -> Good
        d = self.compute({"C1": 100, "C2": 80})
        rows = {r["name"]: r for r in d["campaigns"]}
        g, b = rows["Good camp"], rows["Bad camp"]
        self.assertEqual((g["leads"], g["confirmed"], g["not_shipped"]), (8, 7, 1))
        # 7 shipped x (100 net - 40 cost) = 420 - 100 spend
        self.assertEqual(g["est_profit"], 320.0)
        self.assertEqual(g["verdict"], "scale")
        # 1 kept x 60 - 80 spend - 4 returns
        self.assertEqual(b["est_profit"], -24.0)
        self.assertEqual(b["return_rate_pct"], 80.0)
        self.assertEqual(b["verdict"], "stop")
        self.assertEqual(d["totals"]["stop"], 1)
        # low stock: the ad's product has 0 units in stock
        self.assertEqual(g["low_stock_products"][0]["product"], "Doudoune")
        json.dumps(d)

    def test_too_early_is_watch(self):
        self.order("en_cours", "Bad camp")
        d = self.compute({"C2": 10})
        b = {r["name"]: r for r in d["campaigns"]}["Bad camp"]
        self.assertEqual(b["verdict"], "watch")


class UntrackedCampaignTests(MarketingFiguresTests):
    def test_unlinked_campaign_with_no_orders_is_never_stop(self):
        d = self.compute({"C2": 400})           # Bad camp: no link, no order
        b = {r["name"]: r for r in d["campaigns"]}["Bad camp"]
        self.assertEqual(b["verdict"], "untracked")
        self.assertEqual(d["totals"]["untracked_spend"], 400.0)

    def test_linked_campaign_with_no_orders_can_be_stop(self):
        d = self.compute({"C1": 400})           # Good camp is linked to an offer
        g = {r["name"]: r for r in d["campaigns"]}["Good camp"]
        self.assertEqual(g["verdict"], "stop")


class ChatTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_superuser("owner2", "o2@example.com", "pw")
        prof = getattr(self.owner, "profile", None)
        if prof is not None and prof.must_change_password:
            prof.must_change_password = False
            prof.save()
        self.emp = AIEmployee.objects.get(slug="finance")
        AITask.objects.create(employee=self.emp, title="Bénéfice / pertes — Hier",
                              status=AITask.DONE, period_from=DAY, period_to=DAY,
                              result_data={"net_profit": 691.1}, result_text="ربحت 691")
        self.client.force_login(self.owner)

    def post(self, body):
        return self.client.post("/ai-office/api/employees/finance/chat/", json.dumps(body),
                                content_type="application/json")

    def test_conversation_remembers_and_uses_last_report(self):
        calls = []

        def fake(system, messages, model, **kw):
            calls.append((system, [dict(m) for m in messages]))
            return f"جواب {len(messages)}"

        with patch.object(ai_office, "_claude_chat", side_effect=fake):
            r1 = self.post({"message": "علاش؟"}).json()
            r2 = self.post({"message": "وشنوة نعمل؟"}).json()
        self.assertEqual(r1["reply"], "جواب 1")
        self.assertEqual(r2["reply"], "جواب 3")          # saw user, assistant, user
        self.assertEqual([m["role"] for m in r2["messages"]],
                         ["user", "assistant", "user", "assistant"])
        system = calls[0][0]
        self.assertIn("691.1", system)                    # last report figures
        self.assertIn("VOIX HAUTE", system)
        self.assertEqual(self.client.get("/ai-office/api/employees/finance/chat/").json()["messages"][-1]["text"], "جواب 3")

    def test_new_conversation_starts_empty(self):
        with patch.object(ai_office, "_claude_chat", return_value="ok"):
            self.post({"message": "salam"})
        self.assertEqual(self.post({"new": True}).json()["messages"], [])
        self.assertEqual(self.client.get("/ai-office/api/employees/finance/chat/").json()["messages"], [])

    def test_ai_failure_keeps_owner_message(self):
        with patch.object(ai_office, "_claude_chat", return_value=None):
            r = self.post({"message": "salam"})
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.json()["messages"][-1]["text"], "salam")

    def test_empty_message_and_owner_only(self):
        self.assertEqual(self.post({"message": "  "}).status_code, 400)
        self.client.logout()
        self.assertEqual(self.post({"message": "x"}).status_code, 302)


class PagesDailyTests(TestCase):
    """Sami's per-page, per-day view = the Statistiques > Pages numbers."""

    def setUp(self):
        self.p = Product.objects.create(name="Pull", code="PLL", buy_price=Decimal("40"),
                                        sell_price=Decimal("100"))
        self.v = ProductVariant.objects.create(product=self.p, color_name="RED", color_label="Rouge")
        self.cust = Customer.objects.create(phone="21000000", name="T")
        self.page, _ = SalesPage.objects.get_or_create(name="Arrow.tn")
        self.n = 0

    def order(self, day, status):
        self.n += 1
        o = Order.objects.create(customer=self.cust, sales_page=self.page, status=status,
                                 total=Decimal("107"), delivery_fee=Decimal("7"))
        Order.objects.filter(pk=o.pk).update(created_at=_at(day, 10))   # 10h = that business day
        OrderLine.objects.create(order=o, product=self.p, variant=self.v, size="M",
                                 quantity=1, unit_price=Decimal("100"))
        ShippingOrder.objects.create(bordereau_barcode=f"BX{self.n}", order=o,
                                     status=ShippingOrder.CLOSED)   # it went out (sortie)
        return o

    def test_closed_and_open_days(self):
        closed_day, open_day = DAY - timedelta(days=5), DAY
        self.order(closed_day, "payee"); self.order(closed_day, "payee")
        self.order(closed_day, "returned")
        self.order(open_day, "payee"); self.order(open_day, "en_cours")
        spend = {closed_day: ({"Arrow.tn": Decimal("30")}, {"Arrow.tn": 1}),
                 open_day: ({"Arrow.tn": Decimal("20")}, {"Arrow.tn": 1})}
        with patch.object(ai_office, "_prefetch_page_spend",
                          side_effect=lambda days: {d: spend.get(d, ({}, {})) for d in days}):
            d = ai_office.compute_pages_daily(closed_day, open_day)
        days = {x["day"]: x for x in d["days"]}
        c, o = days[closed_day.isoformat()], days[open_day.isoformat()]
        self.assertEqual(c["status"], "closed_exact")
        self.assertEqual(o["status"], "open_provisional")
        # closed: 2 paid x (100 - 40) - 30 pub = 90 ; cost/order on paid orders
        self.assertEqual(c["profit_day"], 90.0)
        self.assertEqual(c["cost_per_order_basis"], "payée+livrée")
        self.assertEqual(c["cost_per_order"], 15.0)
        self.assertEqual(c["pages"][0]["page"], "Arrow.tn")
        # open: 1 paid x 60 - 20 = 40, cost/order still on sortie (2 out)
        self.assertEqual(o["profit_day"], 40.0)
        self.assertEqual(o["cost_per_order_basis"], "sortie")
        self.assertEqual(o["cost_per_order"], 10.0)
        self.assertEqual((d["closed_days"], d["open_days"]), (1, 1))
        self.assertEqual(d["profit_closed_days_exact"], 90.0)
        json.dumps(d)

    def test_same_numbers_as_stats_page(self):
        self.order(DAY, "payee"); self.order(DAY, "returned")
        from . import views
        with patch.object(views, "_meta_spend_by_page", return_value=({"Arrow.tn": Decimal("25")}, {"Arrow.tn": 1})), \
                patch.object(views, "_cfg", side_effect=lambda k, d="": "tok" if k == "META_ACCESS_TOKEN" else d):
            page_ctx = views._stats_pages_data(DAY, DAY)
            d = ai_office.compute_pages_daily(DAY, DAY)
        self.assertEqual(d["days"][0]["benefit_pages"], float(page_ctx["totals"]["benefit"]))
        self.assertEqual(d["days"][0]["benefit_pages"], 35.0)   # 60 - 25
