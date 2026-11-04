from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from accounts.models import AppAccess, Membership, Organization
from catalog.test_views import member
from catalog.tests import product

from .models import Purchase, PurchaseItem, Supplier
from .tests import receipt

PURCHASING = (AppAccess.App.PURCHASING,)


def line_post(*lines, **header):
    data = {"purchased_on": "2026-10-01", "currency": "ARS", **header}
    for name in ("line_id", "line_product", "line_quantity", "line_unit_cost", "line_supplier_sku"):
        data[name] = []
    for line in lines:
        line = {"line_id": "", "line_supplier_sku": "", **line}
        for name in ("line_id", "line_product", "line_quantity", "line_unit_cost", "line_supplier_sku"):
            data[name].append(line[name])
    return data


class PurchasingAccessTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Seller")
        self.supplier = Supplier.objects.create(organization=self.organization, name="Importadora Sur")
        self.purchase = Purchase.objects.create(
            organization=self.organization, supplier=self.supplier, purchased_on=date(2026, 10, 1)
        )

    def test_without_purchasing_access_every_page_is_closed(self):
        self.client.force_login(member(self.organization, apps=(AppAccess.App.CATALOG,)))
        for url in ("/purchases/", "/purchases/new/", f"/purchases/{self.purchase.pk}/", "/purchases/suppliers/", f"/purchases/suppliers/{self.supplier.pk}/"):
            self.assertEqual(self.client.get(url).status_code, 403, url)

    def test_a_contributor_reads_but_cannot_change_purchases(self):
        self.client.force_login(member(self.organization, role=Membership.Role.CONTRIBUTOR, apps=PURCHASING))
        listing = self.client.get("/purchases/")
        self.assertContains(listing, "Importadora Sur")
        self.assertNotContains(listing, "New purchase")
        page = self.client.get(f"/purchases/{self.purchase.pk}/")
        self.assertContains(page, "Only owners and admins can change purchases.")
        for url in ("/purchases/new/", f"/purchases/{self.purchase.pk}/", "/purchases/suppliers/", f"/purchases/suppliers/{self.supplier.pk}/"):
            self.assertEqual(self.client.post(url, {"name": "X"}).status_code, 403, url)
        self.assertEqual(Supplier.objects.count(), 1)


class PurchasingTenancyTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Seller")
        self.other = Organization.objects.create(name="Other")
        self.client.force_login(member(self.organization, apps=PURCHASING + (AppAccess.App.CASHFLOW,)))
        self.supplier = Supplier.objects.create(organization=self.organization, name="Importadora Sur")
        self.mine = product(self.organization, variant="30ML")
        self.stranger = Supplier.objects.create(organization=self.other, name="Stranger Supplies")
        self.theirs = product(self.other, variant="SECRET")
        self.their_purchase = Purchase.objects.create(
            organization=self.other, supplier=self.stranger, purchased_on=date(2026, 10, 1), invoice_number="THEIRS-1"
        )

    def test_another_organizations_rows_are_never_shown_or_opened(self):
        listing = self.client.get("/purchases/")
        self.assertNotContains(listing, "THEIRS-1")
        self.assertNotContains(self.client.get("/purchases/suppliers/"), "Stranger Supplies")
        self.assertEqual(self.client.get(f"/purchases/{self.their_purchase.pk}/").status_code, 404)
        self.assertEqual(self.client.get(f"/purchases/suppliers/{self.stranger.pk}/").status_code, 404)
        form = self.client.get("/purchases/new/")
        self.assertNotContains(form, "SECRET")
        self.assertNotContains(form, "Stranger Supplies")
        self.assertContains(form, "PERF-ONLY-885-30ML")

    def test_a_purchase_cannot_use_another_organizations_supplier_product_or_receipt(self):
        theirs = self.client.post(
            "/purchases/new/",
            line_post({"line_product": self.theirs.sku, "line_quantity": "1", "line_unit_cost": "1"}, supplier=self.supplier.pk),
        )
        self.assertContains(theirs, "Pick a product from the catalog by its SKU.")
        stranger = self.client.post(
            "/purchases/new/",
            line_post({"line_product": self.mine.sku, "line_quantity": "1", "line_unit_cost": "1"}, supplier=self.stranger.pk),
        )
        self.assertContains(stranger, "Select a valid choice")
        their_receipt = receipt(self.other)
        linked = self.client.post(
            "/purchases/new/",
            line_post(
                {"line_product": self.mine.sku, "line_quantity": "1", "line_unit_cost": "1"},
                supplier=self.supplier.pk,
                cashflow_movement=their_receipt.pk,
            ),
        )
        self.assertContains(linked, "Select a valid choice")
        self.assertFalse(Purchase.objects.filter(organization=self.organization).exists())

    def test_a_line_id_from_another_purchase_is_not_moved(self):
        their_line = PurchaseItem.objects.create(
            purchase=self.their_purchase, product=self.theirs, quantity=1, unit_cost=Decimal("5")
        )
        self.client.post(
            "/purchases/new/",
            line_post(
                {"line_id": str(their_line.pk), "line_product": self.mine.sku, "line_quantity": "2", "line_unit_cost": "10"},
                supplier=self.supplier.pk,
            ),
        )
        their_line.refresh_from_db()
        self.assertEqual((their_line.purchase_id, their_line.product_id), (self.their_purchase.pk, self.theirs.pk))
        mine = Purchase.objects.get(organization=self.organization)
        self.assertEqual(mine.items.get().product, self.mine)


class PurchaseFlowTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Seller")
        self.user = member(self.organization, apps=PURCHASING + (AppAccess.App.CATALOG, AppAccess.App.CASHFLOW))
        self.client.force_login(self.user)
        self.small = product(self.organization, variant="30ML")
        self.large = product(self.organization, variant="100ML")

    def test_suppliers_then_a_purchase_with_lines_then_edits(self):
        self.client.post("/purchases/suppliers/", {"name": "Importadora Sur", "tax_id": "30-71234567-8"})
        same = self.client.post("/purchases/suppliers/", {"name": "Importadora Sur"})
        self.assertContains(same, "already exists")
        supplier = Supplier.objects.get()
        paid = receipt(self.organization, day=timezone.localdate())
        response = self.client.post(
            "/purchases/new/",
            line_post(
                {"line_product": f"{self.small.sku} · {self.small.title}", "line_quantity": "10", "line_unit_cost": "12000,50", "line_supplier_sku": "OS-885-30"},
                {"line_product": self.large.sku.lower(), "line_quantity": "4", "line_unit_cost": "21000"},
                {"line_product": "", "line_quantity": "", "line_unit_cost": ""},
                supplier=supplier.pk,
                invoice_number="A-0001-00001234",
                cashflow_movement=paid.pk,
            ),
        )
        purchase = Purchase.objects.get()
        self.assertRedirects(response, f"/purchases/{purchase.pk}/?saved=1")
        self.assertEqual(purchase.total(), Decimal("204005.00"))
        self.assertEqual(purchase.cashflow_movement, paid)
        self.assertEqual(purchase.items.get(product=self.small).supplier_sku, "OS-885-30")
        listing = self.client.get("/purchases/")
        self.assertContains(listing, "ARS 204005.00")
        self.assertContains(listing, "Receipt")
        cost = self.client.get(f"/catalog/products/{self.small.pk}/")
        self.assertContains(cost, "ARS 12000.50")

        small_line = purchase.items.get(product=self.small)
        edited = self.client.post(
            f"/purchases/{purchase.pk}/",
            line_post(
                {"line_id": str(small_line.pk), "line_product": self.small.sku, "line_quantity": "12", "line_unit_cost": "11000"},
                supplier=supplier.pk,
                invoice_number="A-0001-00001234",
            ),
        )
        self.assertEqual(edited.status_code, 302)
        purchase.refresh_from_db()
        self.assertEqual([(line.pk, line.quantity) for line in purchase.items.all()], [(small_line.pk, 12)])
        self.assertIsNone(purchase.cashflow_movement)
        history = self.client.get(f"/purchases/suppliers/{supplier.pk}/")
        self.assertContains(history, "A-0001-00001234")

    def test_bad_lines_keep_what_was_typed(self):
        supplier = Supplier.objects.create(organization=self.organization, name="Importadora Sur")
        response = self.client.post(
            "/purchases/new/",
            line_post(
                {"line_product": "PERF-NOPE-1-X", "line_quantity": "0", "line_unit_cost": "abc"},
                supplier=supplier.pk,
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Line 1: Pick a product from the catalog by its SKU.")
        self.assertContains(response, "Quantity must be a whole number")
        self.assertContains(response, "Unit cost must be a number")
        self.assertContains(response, 'value="PERF-NOPE-1-X"')
        empty = self.client.post("/purchases/new/", line_post(supplier=supplier.pk))
        self.assertContains(empty, "Add at least one line.")
        self.assertFalse(Purchase.objects.exists())

    def test_receipts_are_offered_only_with_cashflow_access(self):
        receipt(self.organization, day=timezone.localdate())
        self.assertContains(self.client.get("/purchases/new/"), "Cashflow receipt")
        AppAccess.objects.filter(user=self.user, app=AppAccess.App.CASHFLOW).delete()
        self.assertNotContains(self.client.get("/purchases/new/"), "Cashflow receipt")

    def test_filters_narrow_the_purchases(self):
        north = Supplier.objects.create(organization=self.organization, name="Mayorista Norte")
        south = Supplier.objects.create(organization=self.organization, name="Importadora Sur")
        Purchase.objects.create(organization=self.organization, supplier=north, purchased_on=date(2026, 9, 1), invoice_number="N-1")
        Purchase.objects.create(organization=self.organization, supplier=south, purchased_on=date(2026, 10, 15), invoice_number="S-1")
        by_supplier = self.client.get(f"/purchases/?supplier={north.pk}", HTTP_HX_REQUEST="true")
        self.assertContains(by_supplier, "N-1")
        self.assertNotContains(by_supplier, "S-1")
        by_date = self.client.get("/purchases/?date_from=2026-10-01")
        self.assertContains(by_date, "S-1")
        self.assertNotContains(by_date, "N-1")
