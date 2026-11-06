from decimal import Decimal

from django.test import TestCase

from accounts.models import AppAccess, Membership, Organization
from catalog.test_views import member
from catalog.tests import product
from inventory import ledger
from tasks.models import CashflowMovement

from .models import Purchase, PurchaseItem, Supplier
from .tests import receipt


class PurchaseDeleteTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Seller")
        self.client.force_login(member(self.organization, apps=(AppAccess.App.PURCHASING, AppAccess.App.CASHFLOW)))
        self.supplier = Supplier.objects.create(organization=self.organization, name="Sur")
        self.movement = receipt(self.organization)
        self.purchase = Purchase.objects.create(
            organization=self.organization, supplier=self.supplier, purchased_on="2026-11-01",
            cashflow_movement=self.movement,
        )
        self.line = PurchaseItem.objects.create(
            purchase=self.purchase, product=product(self.organization), quantity=3, unit_cost=Decimal("10")
        )

    def test_an_unreceived_purchase_is_deleted_and_its_receipt_stays(self):
        page = self.client.get(f"/purchases/{self.purchase.pk}/")
        self.assertContains(page, "Its cashflow receipt stays in Cashflow, unlinked.")
        response = self.client.post(f"/purchases/{self.purchase.pk}/", {"action": "delete"}, follow=True)
        self.assertRedirects(response, "/purchases/")
        self.assertFalse(Purchase.objects.exists())
        self.assertFalse(PurchaseItem.objects.exists())
        self.assertTrue(CashflowMovement.objects.filter(pk=self.movement.pk).exists())

    def test_a_received_purchase_is_refused(self):
        ledger.start(self.organization)
        ledger.receive(self.purchase, {self.line.pk: 2})
        self.assertContains(self.client.get(f"/purchases/{self.purchase.pk}/"), "2 units were received")
        response = self.client.post(f"/purchases/{self.purchase.pk}/", {"action": "delete"}, follow=True)
        self.assertContains(response, "wasn&#x27;t deleted")
        self.assertTrue(Purchase.objects.exists())

    def test_a_supplier_with_purchases_is_refused_until_they_go(self):
        self.client.post(f"/purchases/suppliers/{self.supplier.pk}/", {"action": "delete"})
        self.assertTrue(Supplier.objects.exists())
        self.purchase.delete()
        self.client.post(f"/purchases/suppliers/{self.supplier.pk}/", {"action": "delete"})
        self.assertFalse(Supplier.objects.exists())

    def test_only_editors_and_only_their_organization(self):
        self.client.force_login(
            member(self.organization, role=Membership.Role.CONTRIBUTOR, username="cid", apps=(AppAccess.App.PURCHASING,))
        )
        self.assertEqual(self.client.post(f"/purchases/{self.purchase.pk}/", {"action": "delete"}).status_code, 403)
        other = Organization.objects.create(name="Other")
        self.client.force_login(member(other, username="bea", apps=(AppAccess.App.PURCHASING,)))
        self.assertEqual(self.client.post(f"/purchases/{self.purchase.pk}/", {"action": "delete"}).status_code, 404)
        self.assertTrue(Purchase.objects.exists())
