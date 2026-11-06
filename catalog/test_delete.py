from decimal import Decimal

from django.test import TestCase

from accounts.models import AppAccess, Membership, Organization
from inventory import ledger
from inventory.tests import listing
from purchasing.models import Purchase, PurchaseItem, Supplier

from .models import Brand, Combo, ComboItem, MasterProduct, ProductModel
from .test_views import member
from .tests import IMAGE, product


class DeleteTests(TestCase):
    """Owners and admins delete what nothing else needs; the rest is refused, or archived."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Seller")
        self.client.force_login(member(self.organization))
        self.item = product(self.organization, variant="30ML", list_price=Decimal("1000"))

    def test_an_unused_product_is_deleted_and_its_listings_unlinked(self):
        row = listing(self.organization, "MLA1", self.item)
        page = self.client.get(f"/catalog/products/{self.item.pk}/")
        self.assertContains(page, "1 Mercado Libre listing stay on Mercado Libre")
        response = self.client.post(f"/catalog/products/{self.item.pk}/", {"action": "delete"}, follow=True)
        self.assertRedirects(response, "/catalog/")
        self.assertContains(response, f"{self.item.sku} deleted.")
        self.assertFalse(MasterProduct.objects.filter(pk=self.item.pk).exists())
        row.refresh_from_db()
        self.assertIsNone(row.product_id)

    def test_a_product_with_history_is_refused_and_can_be_archived(self):
        supplier = Supplier.objects.create(organization=self.organization, name="Sur")
        purchase = Purchase.objects.create(organization=self.organization, supplier=supplier, purchased_on="2026-11-01")
        PurchaseItem.objects.create(purchase=purchase, product=self.item, quantity=2, unit_cost=Decimal("10"))
        ledger.start(self.organization)
        ledger.record(self.organization, self.item, ledger.Kind.OPENING, ledger.OWN, 3)
        page = self.client.get(f"/catalog/products/{self.item.pk}/")
        self.assertContains(page, "1 purchase line, 1 stock movement")
        self.assertContains(page, 'value="archive"')
        response = self.client.post(f"/catalog/products/{self.item.pk}/", {"action": "delete"}, follow=True)
        self.assertContains(response, "wasn&#x27;t deleted: 1 purchase line, 1 stock movement")
        self.assertTrue(MasterProduct.objects.filter(pk=self.item.pk).exists())
        self.client.post(f"/catalog/products/{self.item.pk}/", {"action": "archive"})
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, MasterProduct.Status.ARCHIVED)

    def test_a_model_with_variants_and_a_brand_in_use_are_refused(self):
        model = self.item.product_model
        self.client.post(f"/catalog/models/{model.pk}/", {"action": "delete"})
        self.assertTrue(ProductModel.objects.filter(pk=model.pk).exists())
        brand = model.brand
        self.client.post("/catalog/codes/", {"kind": "brand", "id": brand.pk, "action": "delete"})
        self.assertTrue(Brand.objects.filter(pk=brand.pk).exists())
        self.item.delete()
        self.client.post(f"/catalog/models/{model.pk}/", {"action": "delete"})
        self.assertFalse(ProductModel.objects.filter(pk=model.pk).exists())
        self.client.post("/catalog/codes/", {"kind": "brand", "id": brand.pk, "action": "delete"})
        self.assertFalse(Brand.objects.filter(pk=brand.pk).exists())

    def test_a_combo_goes_with_its_lines(self):
        other = product(self.organization, variant="50ML")
        combo = Combo.objects.create(organization=self.organization, code="MIX", title="Kit mix", image_url=IMAGE)
        ComboItem.objects.create(combo=combo, product=self.item, quantity=1)
        ComboItem.objects.create(combo=combo, product=other, quantity=1)
        self.client.post(f"/catalog/combos/{combo.pk}/", {"action": "delete"})
        self.assertFalse(Combo.objects.exists())
        self.assertFalse(ComboItem.objects.exists())

    def test_only_editors_and_only_their_organization(self):
        self.client.force_login(member(self.organization, role=Membership.Role.CONTRIBUTOR, username="cid"))
        self.assertEqual(self.client.post(f"/catalog/products/{self.item.pk}/", {"action": "delete"}).status_code, 403)
        self.assertNotContains(self.client.get(f"/catalog/products/{self.item.pk}/"), 'data-dialog="delete-dialog"')
        other = Organization.objects.create(name="Other")
        self.client.force_login(member(other, username="bea", apps=(AppAccess.App.CATALOG,)))
        self.assertEqual(self.client.post(f"/catalog/products/{self.item.pk}/", {"action": "delete"}).status_code, 404)
        self.assertTrue(MasterProduct.objects.filter(pk=self.item.pk).exists())
