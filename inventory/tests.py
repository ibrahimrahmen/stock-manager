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
