"""
Tests for the "en cours" customer SMS, focused on the bug where the driver
(livreur) phone number was missing from the message.

Root cause covered here: the en-cours SMS is fired from api_navex_sync, which
holds the fresh Navex response (including livreur_tel) but previously never
persisted it onto the linked v2 Order before sending. _maybe_send_status_sms
then read the still-empty Order.navex_livreur_tel, so the number was dropped.
"""
import json
import urllib.request

from django.test import TestCase
from django.test.client import RequestFactory
from django.contrib.auth.models import AnonymousUser

from inventory import views, sms_service
from inventory.models import Customer, Order, ShippingOrder


class _FakeResponse:
    """Minimal stand-in for urllib's response, usable as a context manager."""
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class EnCoursSmsLivreurTest(TestCase):
    BORDEREAU = "977452543171"
    LIVREUR_TEL = "26527710"

    def setUp(self):
        self.factory = RequestFactory()
        self.customer = Customer.objects.create(phone="20123456", name="Test Client")
        # v2 Order, confirmed, waiting to move to "en cours".
        self.order = Order.objects.create(
            customer=self.customer,
            status=Order.CONFIRMEE,
            total=86,
            bordereau_barcode=self.BORDEREAU,
        )
        # v1 ShippingOrder, CLOSED (api_navex_sync only syncs CLOSED orders),
        # linked to the v2 Order.
        self.shipping = ShippingOrder.objects.create(
            bordereau_barcode=self.BORDEREAU,
            status=ShippingOrder.CLOSED,
            order=self.order,
        )
        # Capture every SMS that would be sent, instead of hitting the network.
        self.sent = []

        def _fake_send(phone, message):
            self.sent.append((phone, message))
            return (True, "ok")

        self._orig_send = sms_service.send_sms
        sms_service.send_sms = _fake_send

        # Fake the Navex API call inside api_navex_sync.
        self._orig_urlopen = urllib.request.urlopen

        def _fake_urlopen(req, timeout=None):
            return _FakeResponse({
                "status": 1,
                "total": 1,
                "results": [{
                    "status": 1,
                    "code": self.BORDEREAU,
                    "etat": "En cours",
                    "motif": "",
                    "pre_etat": "",
                    "livreur": "Navex Bizerte",
                    "livreur_tel": self.LIVREUR_TEL,
                    "prix": "86.0",
                }],
            })

        urllib.request.urlopen = _fake_urlopen

    def tearDown(self):
        sms_service.send_sms = self._orig_send
        urllib.request.urlopen = self._orig_urlopen

    def _run_sync(self):
        request = self.factory.post("/api/navex/sync/")
        request.user = AnonymousUser()
        return views.api_navex_sync(request)

    def test_en_cours_sms_includes_livreur_number(self):
        self._run_sync()

        # Exactly one SMS, and it must contain the driver phone number.
        self.assertEqual(len(self.sent), 1, f"expected 1 SMS, got {self.sent}")
        phone, message = self.sent[0]
        self.assertEqual(phone, self.customer.phone)
        self.assertIn(self.LIVREUR_TEL, message,
                      f"livreur number missing from SMS: {message!r}")

        # The order should now be EN_COURS with the driver phone persisted,
        # and the en-cours dedup flag set.
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.EN_COURS)
        self.assertEqual(self.order.navex_livreur_tel, self.LIVREUR_TEL)
        self.assertTrue(self.order.sms_en_cours_sent)
        self.assertEqual(self.order.sms_en_cours_last_tel, self.LIVREUR_TEL)

    def test_second_sync_same_attempt_does_not_resend(self):
        # First sync sends once.
        self._run_sync()
        self.assertEqual(len(self.sent), 1)
        # Second sync, same livreur + same day: must NOT re-send.
        self._run_sync()
        self.assertEqual(len(self.sent), 1,
                         f"should not resend on same attempt: {self.sent}")


class AngryOrderFlagTest(TestCase):
    """The is_angry flag is set automatically from the conversation text
    whenever it's written (creation or update), keyword-based, no AI cost."""

    def setUp(self):
        self.customer = Customer.objects.create(phone="20999888", name="Test")

    def test_angry_conversation_is_flagged_on_create(self):
        o = Order.objects.create(
            customer=self.customer,
            conversation_text="Client: hasbi allah 3lik, quelle arnaque!",
        )
        o.refresh_from_db()
        self.assertTrue(o.is_angry)

    def test_normal_conversation_is_not_flagged(self):
        o = Order.objects.create(
            customer=self.customer,
            conversation_text="Client: bonjour, je veux annuler la commande svp",
        )
        o.refresh_from_db()
        self.assertFalse(o.is_angry)  # "annuler" must not trip on "nul"

    def test_flag_updates_when_conversation_changes(self):
        o = Order.objects.create(customer=self.customer, conversation_text="ok merci")
        o.refresh_from_db()
        self.assertFalse(o.is_angry)
        # An angry follow-up arrives; saved via update_fields (like the webhook).
        o.conversation_text = "Client: ya kelb, nechki 3likom"
        o.save(update_fields=["conversation_text", "updated_at"])
        o.refresh_from_db()
        self.assertTrue(o.is_angry)  # update_fields path must persist is_angry


class StatsModelesTest(TestCase):
    """Per-model stats count UNITS and bucket by the order's status."""

    def setUp(self):
        from django.contrib.auth.models import User
        from inventory.models import (Product, Customer, Order, OrderLine,
                                       ShippingOrder)
        self.admin = User.objects.create_superuser("admin", "a@a.com", "pw")
        self.client.force_login(self.admin)
        self.P = Product
        self.C = Customer
        self.O = Order
        self.OL = OrderLine
        self.SO = ShippingOrder

    def _row(self, resp, name):
        return next((r for r in resp.context["per_model"] if r["product"] == name), None)

    def test_units_counted_bucketed_and_per_page(self):
        from inventory.models import SalesPage
        p = self.P.objects.create(name="Pants ICY Maze", code="PICY")
        c = self.C.objects.create(phone="20111222")
        sp1 = SalesPage.objects.create(name="Barats.tn")
        sp2 = SalesPage.objects.create(name="Insta Store")
        # delivered order on Barats: 3 units + shipping order (Sortie)
        o1 = self.O.objects.create(customer=c, status=self.O.LIVREE, sales_page=sp1)
        self.OL.objects.create(order=o1, product=p, quantity=3, unit_price=0)
        self.SO.objects.create(bordereau_barcode="999000111222", order=o1)
        # returned order on Insta: 1 unit
        o2 = self.O.objects.create(customer=c, status=self.O.RETURNED, sales_page=sp2)
        self.OL.objects.create(order=o2, product=p, quantity=1, unit_price=0)

        resp = self.client.get("/statistiques/modeles/",
                               {"from": "2020-01-01", "to": "2035-01-01", "source": "all"})
        self.assertEqual(resp.status_code, 200)
        row = self._row(resp, "Pants ICY Maze")
        self.assertIsNotNone(row)
        self.assertEqual(row["total"], 4)     # 3 + 1 units
        self.assertEqual(row["livree"], 3)
        self.assertEqual(row["retour"], 1)
        self.assertEqual(row["sortie"], 3)    # only o1 has a shipping order
        self.assertEqual(row["retour_pct"], 33.3)   # 1/3 vs Sortie

        # Per-page drill-down: two pages, each with the right split.
        pages = {pg["page"]: pg for pg in row["pages"]}
        self.assertEqual(set(pages), {"Barats.tn", "Insta Store"})
        self.assertEqual(pages["Barats.tn"]["livree"], 3)
        self.assertEqual(pages["Barats.tn"]["sortie"], 3)
        self.assertEqual(pages["Insta Store"]["retour"], 1)
        self.assertEqual(pages["Insta Store"]["total"], 1)


class StatsGouvernoratsTest(TestCase):
    """Per-governorate stats count ORDERS, bucket by status, show all 24
    governorates (even empty), and drill down per city (ville)."""

    def setUp(self):
        from django.contrib.auth.models import User
        self.admin = User.objects.create_superuser("gadmin", "g@a.com", "pw")
        self.client.force_login(self.admin)

    def _row(self, resp, gov):
        return next((r for r in resp.context["per_gov"] if r["gov"] == gov), None)

    def test_orders_counted_by_governorate_and_city(self):
        from inventory.models import Customer, Order, Region, ShippingOrder
        c = Customer.objects.create(phone="20333444")
        sfax = Region.objects.get(name="Sfax")
        tunis = Region.objects.filter(name__icontains="Tunis").first()
        # Sfax: one delivered (with Sortie) in Sfax city, one returned in Sakiet.
        o1 = Order.objects.create(customer=c, status=Order.LIVREE, region=sfax, ville="Sfax")
        ShippingOrder.objects.create(bordereau_barcode="111222333444", order=o1)
        Order.objects.create(customer=c, status=Order.RETURNED, region=sfax, ville="Sakiet")
        # Tunis: one en_cours.
        Order.objects.create(customer=c, status=Order.EN_COURS, region=tunis, ville="")
        # No region -> "Non spécifié".
        Order.objects.create(customer=c, status=Order.LIVREE, region=None, ville="?")

        resp = self.client.get("/statistiques/gouvernorats/",
                               {"from": "2020-01-01", "to": "2035-01-01", "source": "all"})
        self.assertEqual(resp.status_code, 200)

        # All 24 governorates are present, plus the "Non spécifié" bucket.
        self.assertGreaterEqual(len([r for r in resp.context["per_gov"]
                                     if r["gov"] != "Non spécifié"]), 24)

        sf = self._row(resp, "Sfax")
        self.assertEqual(sf["total"], 2)      # 2 orders in Sfax
        self.assertEqual(sf["livree"], 1)
        self.assertEqual(sf["retour"], 1)
        self.assertEqual(sf["sortie"], 1)     # only o1 has a shipping order
        self.assertEqual(sf["retour_pct"], 100.0)   # 1 retour / 1 sortie
        # City drill-down splits Sfax into its two cities.
        cities = {p["city"]: p for p in sf["cities"]}
        self.assertEqual(set(cities), {"Sfax", "Sakiet"})
        self.assertEqual(cities["Sfax"]["livree"], 1)
        self.assertEqual(cities["Sakiet"]["retour"], 1)

        # A governorate with no orders shows all-zero.
        empty = next(r for r in resp.context["per_gov"]
                     if r["gov"] not in ("Sfax", "Non spécifié") and r["total"] == 0)
        self.assertEqual(empty["retour_pct"], 0.0)

        # Null-region orders land in the "Non spécifié" bucket.
        ns = self._row(resp, "Non spécifié")
        self.assertIsNotNone(ns)
        self.assertEqual(ns["total"], 1)


class UnifunlSyncTest(TestCase):
    """Pull orders from the Unifunl API and import the missing ones as pending
    orders, idempotently. The heavy order engine is stubbed so the test stays
    offline and focused on the sync (fetch / transform / dedup / pagination)."""

    UID = "6ffb37ee-9644-4ed6-b666-6221fcd7d3f9"

    def _envelope(self):
        return {
            "data": [{
                "id": self.UID, "orderNumber": "000001", "storeType": "manual",
                "status": "pending", "customerFirstName": "Jouini",
                "customerLastName": None, "customerPhone": "21623364111",
                "subtotal": 89, "shippingTotal": 7, "total": 96, "currency": "TND",
                "lineItems": [{
                    "name": "Ensemble Premium Casa - Taille: XL", "price": "89.00",
                    "total": 89, "quantity": 1}],
                "shippingAddress": {"city": "Tunis", "phone": "21623364111",
                                    "country": "TN", "address1": "Ibn khaldoun",
                                    "firstName": "Jouini"},
            }],
            "meta": {"hasNextPage": False},
        }

    def test_transform_maps_fields_and_splits_size(self):
        o = self._envelope()["data"][0]
        shaped = views._unifunl_to_shopify_shaped(o)
        self.assertEqual(shaped["shipping_address"]["phone"], "21623364111")
        self.assertEqual(shaped["shipping_address"]["city"], "Tunis")
        li = shaped["line_items"][0]
        self.assertEqual(li["title"], "Ensemble Premium Casa")   # size stripped
        self.assertEqual(li["variant_title"], "XL")              # size extracted
        self.assertEqual(li["quantity"], 1)
        self.assertEqual(shaped["shipping_lines"][0]["price"], "7")

    def test_sync_creates_then_is_idempotent(self):
        import os
        from inventory.models import Customer, Order, SalesPage

        # Stub the heavy engine: create a minimal pending order carrying the
        # dedup note the sync relies on.
        created_calls = []

        def _fake_engine(payload, source="shopify", external_id="", request=None,
                         sales_page_id=None):
            created_calls.append((source, external_id, sales_page_id))
            cust, _ = Customer.objects.get_or_create(
                phone=payload["shipping_address"]["phone"][-8:])
            sp = SalesPage.objects.filter(pk=sales_page_id).first()
            Order.objects.create(
                customer=cust, sales_page=sp, status=Order.NON_CONFIRMEE,
                source=Order.SOURCE_MESSENGER,
                notes=f"shopify_order_id={external_id}")
            return None

        env = self._envelope()

        def _fake_urlopen(req, timeout=None):
            return _FakeResponse(env)

        orig_engine = views._create_order_from_shopify_shaped_payload
        orig_urlopen = urllib.request.urlopen
        views._create_order_from_shopify_shaped_payload = _fake_engine
        urllib.request.urlopen = _fake_urlopen
        os.environ["UNIFUNL_API_KEY"] = "ufl_test"
        try:
            res1 = views._sync_unifunl_orders(apply=True)
            res2 = views._sync_unifunl_orders(apply=True)   # second run
            dry = views._sync_unifunl_orders(apply=False)
        finally:
            views._create_order_from_shopify_shaped_payload = orig_engine
            urllib.request.urlopen = orig_urlopen
            os.environ.pop("UNIFUNL_API_KEY", None)

        self.assertEqual(res1["created"], 1)
        self.assertEqual(res1["skipped"], 0)
        # Order landed as a pending Unifunl order.
        o = Order.objects.get(notes__contains=f"shopify_order_id=unifunl:{self.UID}")
        self.assertEqual(o.status, Order.NON_CONFIRMEE)
        self.assertEqual(o.sales_page.name, "Barats")   # tagged like normal Barats orders
        self.assertEqual(o.customer.phone, "23364111")   # last 8 digits
        # Second run must NOT re-create (idempotent).
        self.assertEqual(res2["created"], 0)
        self.assertEqual(res2["skipped"], 1)
        self.assertEqual(len(created_calls), 1)
        # Dry-run reports nothing to create once it already exists.
        self.assertEqual(dry["would_create"], [])

    def test_sync_without_key_errors_cleanly(self):
        import os
        os.environ.pop("UNIFUNL_API_KEY", None)
        res = views._sync_unifunl_orders(apply=True)
        self.assertEqual(res["status"], "error")
        self.assertIn("UNIFUNL_API_KEY", res["message"])

    def test_stored_conversation_is_linked_by_phone(self):
        import os
        from inventory.models import Customer, Order, SalesPage, MessengerConversation

        # A conversation captured by our webhook, containing the customer's phone
        # (last 8 digits of the Unifunl phone 21623364111 -> 23364111).
        conv = MessengerConversation.objects.create(
            platform="messenger", page_id="580021675198711", sender_id="PSID123",
            sender_name="Jouini",
            messages=[{"from": "user", "text": "3aslema, 23364111, nheb ncommandi"},
                      {"from": "page", "text": "ahla bik"}])

        def _fake_engine(payload, source="shopify", external_id="", request=None,
                         sales_page_id=None):
            cust, _ = Customer.objects.get_or_create(
                phone=payload["shipping_address"]["phone"][-8:])
            sp = SalesPage.objects.filter(pk=sales_page_id).first()
            Order.objects.create(
                customer=cust, sales_page=sp, status=Order.NON_CONFIRMEE,
                source=Order.SOURCE_MESSENGER,
                notes=f"shopify_order_id={external_id}")
            return None

        def _fake_urlopen(req, timeout=None):
            return _FakeResponse(self._envelope())

        orig_engine = views._create_order_from_shopify_shaped_payload
        orig_urlopen = urllib.request.urlopen
        views._create_order_from_shopify_shaped_payload = _fake_engine
        urllib.request.urlopen = _fake_urlopen
        os.environ["UNIFUNL_API_KEY"] = "ufl_test"
        try:
            views._sync_unifunl_orders(apply=True)
        finally:
            views._create_order_from_shopify_shaped_payload = orig_engine
            urllib.request.urlopen = orig_urlopen
            os.environ.pop("UNIFUNL_API_KEY", None)

        order = Order.objects.get(notes__contains=f"shopify_order_id=unifunl:{self.UID}")
        conv.refresh_from_db()
        # Conversation linked to the order, and its transcript snapshotted.
        self.assertEqual(conv.pending_order_id, order.id)
        self.assertIn("23364111", order.conversation_text)
        self.assertIn("Client:", order.conversation_text)


class NavexNameCleanTest(TestCase):
    """Fancy social-media names (bold/fraktur unicode) and emoji must be folded
    to plain letters, since Navex rejects them. Normal names pass through."""

    def test_folds_fancy_and_strips_emoji(self):
        self.assertEqual(views._navex_clean_text("𝕳𝖆𝖒𝖒𝖒𝖆🪬"), "Hammma")
        self.assertEqual(views._navex_clean_text("𝓙𝓸𝓾𝓲𝓷𝓲 🔥"), "Jouini")

    def test_leaves_normal_names_untouched(self):
        self.assertEqual(views._navex_clean_text("Mohamed Ali"), "Mohamed Ali")
        self.assertEqual(views._navex_clean_text("أحمد"), "أحمد")
