from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models import ProtectedError
from django.test import TestCase

from accounts.models import Organization
from catalog.models import MasterProduct
from catalog.tests import product
from tasks.models import CashflowMovement

from .models import Purchase, PurchaseItem, Supplier


def receipt(organization, day=date(2026, 11, 1)):
    return CashflowMovement.objects.create(
        organization=organization,
        uploader_email="ada@example.com",
        transaction_date=day,
        category="cost",
        amount=Decimal("-150000"),
        currency="ARS",
    )


class PurchaseTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Seller")
        self.other = Organization.objects.create(name="Other")
        self.perfume = product(self.organization, variant="100ML")
        self.small = product(self.organization, variant="50ML")
        self.importer = Supplier.objects.create(organization=self.organization, name="Importadora Sur")
        self.wholesaler = Supplier.objects.create(organization=self.organization, name="Mayorista Norte")

    def _buy(self, supplier, day, *lines, organization=None, **fields):
        purchase = Purchase.objects.create(
            organization=organization or self.organization,
            supplier=supplier,
            purchased_on=day,
            **fields,
        )
        for item, quantity, cost in lines:
            PurchaseItem.objects.create(purchase=purchase, product=item, quantity=quantity, unit_cost=Decimal(cost))
        return purchase

    def test_a_purchase_totals_its_lines_and_lines_take_its_organization(self):
        purchase = self._buy(
            self.importer,
            date(2026, 10, 1),
            (self.perfume, 10, "12000.50"),
            (self.small, 4, "7000"),
        )
        self.assertEqual(purchase.total(), Decimal("148005.00"))
        self.assertEqual(
            set(purchase.items.values_list("organization_id", flat=True)),
            {self.organization.id},
        )

    def test_costs_by_supplier_keeps_the_latest_price_from_each_supplier(self):
        self._buy(self.importer, date(2026, 9, 1), (self.perfume, 5, "11000"))
        self._buy(self.importer, date(2026, 10, 15), (self.perfume, 5, "12500"))
        self._buy(self.wholesaler, date(2026, 10, 1), (self.perfume, 2, "13900"), (self.small, 2, "8000"))
        theirs = product(self.other, variant="100ML")
        stranger = Supplier.objects.create(organization=self.other, name="Importadora Sur")
        self._buy(stranger, date(2026, 11, 1), (theirs, 1, "1"), organization=self.other)

        costs = self.perfume.costs_by_supplier()
        self.assertEqual(
            [(row["supplier"].name, row["unit_cost"], row["purchased_on"]) for row in costs],
            [
                ("Importadora Sur", Decimal("12500.00"), date(2026, 10, 15)),
                ("Mayorista Norte", Decimal("13900.00"), date(2026, 10, 1)),
            ],
        )
        self.assertEqual(theirs.costs_by_supplier()[0]["unit_cost"], Decimal("1.00"))
        self.assertEqual(len(theirs.costs_by_supplier()), 1)

    def test_a_line_cannot_use_another_organizations_product(self):
        purchase = self._buy(self.importer, date(2026, 10, 1))
        theirs = product(self.other, code="999")
        line = PurchaseItem(purchase=purchase, product=theirs, quantity=1, unit_cost=Decimal("1"))
        with self.assertRaises(ValidationError):
            line.full_clean()
        with self.assertRaises(ValidationError):
            line.save()
        self.assertFalse(PurchaseItem.objects.exists())

    def test_a_purchase_cannot_use_another_organizations_supplier_or_receipt(self):
        stranger = Supplier.objects.create(organization=self.other, name="Stranger")
        with self.assertRaises(ValidationError):
            Purchase.objects.create(organization=self.organization, supplier=stranger, purchased_on=date(2026, 10, 1))
        with self.assertRaises(ValidationError):
            Purchase.objects.create(
                organization=self.organization,
                supplier=self.importer,
                purchased_on=date(2026, 10, 1),
                cashflow_movement=receipt(self.other),
            )
        self.assertFalse(Purchase.objects.exists())

    def test_a_receipt_links_and_unlinks_without_losing_the_purchase(self):
        paid = receipt(self.organization)
        purchase = self._buy(self.importer, date(2026, 10, 1), (self.perfume, 1, "100"), cashflow_movement=paid)
        self.assertEqual(list(paid.purchases.all()), [purchase])
        paid.delete()
        purchase.refresh_from_db()
        self.assertIsNone(purchase.cashflow_movement)

    def test_a_product_with_purchases_cannot_be_deleted(self):
        self._buy(self.importer, date(2026, 10, 1), (self.perfume, 1, "100"))
        with self.assertRaises(ProtectedError):
            self.perfume.delete()
        self.assertTrue(MasterProduct.objects.filter(pk=self.perfume.pk).exists())


class AdminPagesTests(TestCase):
    def test_a_superuser_opens_every_catalog_and_purchasing_page(self):
        organization = Organization.objects.create(name="Test Seller")
        item = product(organization)
        supplier = Supplier.objects.create(organization=organization, name="Importadora Sur")
        purchase = Purchase.objects.create(organization=organization, supplier=supplier, purchased_on=date(2026, 10, 1))
        PurchaseItem.objects.create(purchase=purchase, product=item, quantity=2, unit_cost=Decimal("10"))
        admin = get_user_model().objects.create_superuser("root", "root@example.com", "pw")
        self.client.force_login(admin)
        for url in (
            "/admin/catalog/masterproduct/",
            "/admin/catalog/masterproduct/add/",
            f"/admin/catalog/masterproduct/{item.pk}/change/",
            "/admin/purchasing/supplier/",
            "/admin/purchasing/supplier/add/",
            "/admin/purchasing/purchase/",
            "/admin/purchasing/purchase/add/",
            f"/admin/purchasing/purchase/{purchase.pk}/change/",
        ):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        page = self.client.get(f"/admin/catalog/masterproduct/{item.pk}/change/")
        self.assertContains(page, "Importadora Sur")

    def test_the_admin_records_a_purchase_with_its_lines(self):
        organization = Organization.objects.create(name="Test Seller")
        other = Organization.objects.create(name="Other")
        item = product(organization)
        theirs = product(other, code="999")
        supplier = Supplier.objects.create(organization=organization, name="Importadora Sur")
        admin = get_user_model().objects.create_superuser("root", "root@example.com", "pw")
        self.client.force_login(admin)
        form = {
            "organization": organization.pk,
            "supplier": supplier.pk,
            "purchased_on": "2026-10-01",
            "invoice_number": "A-0001-00001234",
            "currency": "ARS",
            "items-TOTAL_FORMS": "1",
            "items-INITIAL_FORMS": "0",
            "items-MIN_NUM_FORMS": "0",
            "items-MAX_NUM_FORMS": "1000",
            "items-0-product": item.pk,
            "items-0-quantity": "3",
            "items-0-unit_cost": "12000",
        }
        response = self.client.post("/admin/purchasing/purchase/add/", form)
        self.assertEqual(response.status_code, 302)
        purchase = Purchase.objects.get()
        line = purchase.items.get()
        self.assertEqual((line.organization_id, line.quantity), (organization.id, 3))

        refused = self.client.post(
            "/admin/purchasing/purchase/add/",
            {**form, "invoice_number": "B", "items-0-product": theirs.pk},
        )
        self.assertEqual(refused.status_code, 200)
        self.assertContains(refused, "This belongs to another organization.")
        self.assertEqual(Purchase.objects.count(), 1)
