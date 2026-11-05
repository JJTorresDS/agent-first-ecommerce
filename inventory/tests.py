import json
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import AppAccess, Membership, Organization
from catalog.test_views import member
from catalog.tests import product
from meli.models import MeliListing, MeliSale
from meli.sales import record_order
from meli.tests import SAMPLE, WEBHOOK_SECRET, WEBHOOK_URL, TokenResponse, shared_seller
from purchasing.models import Purchase, PurchaseItem, Supplier

from . import ledger
from .models import InventorySettings, StockLocation, StockMovement

OWN, FULL = ledger.OWN, ledger.FULL


def listing(organization, item_id, item_product=None, variation_id="", **fields):
    return MeliListing.objects.create(
        organization=organization,
        item_id=item_id,
        variation_id=variation_id,
        product=item_product,
        seller_sku=fields.pop("seller_sku", item_product.sku if item_product else ""),
        synced_at=timezone.now(),
        **fields,
    )


def order(order_id, item_id, quantity=1, status="paid", when=None, variation_id=None):
    when = when or timezone.now()
    return {
        "id": order_id,
        "status": status,
        "date_created": when.isoformat(),
        "date_closed": when.isoformat(),
        "order_items": [
            {"item": {"id": item_id, "variation_id": variation_id, "title": f"Listing {item_id}"}, "quantity": quantity}
        ],
    }


class LedgerTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Seller")
        self.other = Organization.objects.create(name="Other")
        self.item = product(self.organization, variant="30ML")

    def test_on_hand_is_the_sum_per_location(self):
        ledger.record(self.organization, self.item, ledger.Kind.ADJUSTMENT, OWN, 10)
        ledger.record(self.organization, self.item, ledger.Kind.ADJUSTMENT, OWN, -3)
        ledger.record(self.organization, self.item, ledger.Kind.ADJUSTMENT, FULL, 4)
        self.assertEqual(ledger.on_hand(self.item), {OWN: 7, FULL: 4})
        self.assertEqual(ledger.on_hand_by_product(self.organization)[self.item.pk], {OWN: 7, FULL: 4})
        self.assertEqual(StockLocation.objects.filter(organization=self.organization).count(), 2)

    def test_zero_and_other_organizations_are_refused(self):
        with self.assertRaises(ValidationError):
            ledger.record(self.organization, self.item, ledger.Kind.ADJUSTMENT, OWN, 0)
        theirs = product(self.other, variant="30ML")
        with self.assertRaises(ValidationError):
            ledger.record(self.organization, theirs, ledger.Kind.ADJUSTMENT, OWN, 1)
        their_place = ledger.location(self.other, OWN)
        with self.assertRaises(ValidationError):
            StockMovement.objects.create(
                organization=self.organization, product=self.item, location=their_place, quantity=1, kind="adjustment"
            )
        self.assertFalse(StockMovement.objects.exists())

    def test_a_source_is_counted_once(self):
        first = ledger.record(self.organization, self.item, ledger.Kind.SALE, OWN, -1, source_key="sale:1:MLA1:")
        again = ledger.record(self.organization, self.item, ledger.Kind.SALE, OWN, -1, source_key="sale:1:MLA1:")
        self.assertIsNotNone(first)
        self.assertIsNone(again)
        self.assertEqual(ledger.on_hand(self.item)[OWN], -1)

    def test_a_count_writes_the_difference_and_the_first_one_starts(self):
        self.assertTrue(ledger.start(self.organization, by="ada@example.com"))
        self.assertFalse(ledger.start(self.organization))
        opening = ledger.count(self.organization, self.item, OWN, 12, opening=True)
        self.assertEqual((opening.kind, opening.quantity), ("opening", 12))
        self.assertIsNone(ledger.count(self.organization, self.item, OWN, 12))
        later = ledger.count(self.organization, self.item, OWN, 9)
        self.assertEqual((later.kind, later.quantity), ("count", -3))
        self.assertEqual(InventorySettings.objects.get(organization=self.organization).started_by, "ada@example.com")

    def test_adjustments_and_sending_to_full(self):
        ledger.adjust(self.organization, self.item, ledger.Kind.RETURN, -2)
        ledger.adjust(self.organization, self.item, ledger.Kind.DAMAGE, 1)
        ledger.adjust(self.organization, self.item, ledger.Kind.ADJUSTMENT, -4)
        self.assertEqual(ledger.on_hand(self.item)[OWN], -3)
        ledger.adjust(self.organization, self.item, ledger.Kind.ADJUSTMENT, 13)
        ledger.send_to_full(self.organization, self.item, 6)
        self.assertEqual(ledger.on_hand(self.item), {OWN: 4, FULL: 6})
        with self.assertRaises(ValidationError):
            ledger.send_to_full(self.organization, self.item, 0)
        with self.assertRaises(ValidationError):
            ledger.adjust(self.organization, self.item, ledger.Kind.SALE, 1)


class ReceivingTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Seller")
        self.client.force_login(member(self.organization, apps=(AppAccess.App.PURCHASING, AppAccess.App.CATALOG)))
        self.item = product(self.organization, variant="30ML")
        supplier = Supplier.objects.create(organization=self.organization, name="Importadora Sur")
        self.purchase = Purchase.objects.create(organization=self.organization, supplier=supplier, purchased_on=date(2026, 10, 1))
        self.line = PurchaseItem.objects.create(purchase=self.purchase, product=self.item, quantity=10, unit_cost=Decimal("100"))

    def _receive(self, units):
        return self.client.post(
            f"/purchases/{self.purchase.pk}/",
            {"action": "receive", f"receive_{self.line.pk}": str(units), "received_on": "2026-10-05"},
        )

    def test_receiving_in_parts_adds_stock_and_moves_the_status(self):
        self.assertContains(self.client.get(f"/purchases/{self.purchase.pk}/"), "Ordered")
        self.assertRedirects(self._receive(4), f"/purchases/{self.purchase.pk}/?received=1")
        self.assertEqual(ledger.on_hand(self.item)[OWN], 4)
        self.assertContains(self.client.get("/purchases/"), "Partly received")
        self._receive(6)
        self.assertEqual(ledger.on_hand(self.item)[OWN], 10)
        self.assertContains(self.client.get(f"/purchases/{self.purchase.pk}/"), "Received")
        movement = StockMovement.objects.filter(kind="receipt").first()
        self.assertEqual(movement.purchase_item, self.line)
        self.assertEqual(movement.occurred_at.date(), date(2026, 10, 5))

    def test_more_than_ordered_is_refused(self):
        self._receive(8)
        too_many = self._receive(3)
        self.assertContains(too_many, "2 still to receive")
        self.assertEqual(ledger.on_hand(self.item)[OWN], 8)

    def test_a_received_line_cannot_shrink_change_or_go(self):
        self._receive(5)
        header = {"supplier": self.purchase.supplier_id, "purchased_on": "2026-10-01", "currency": "ARS"}
        lower = self.client.post(
            f"/purchases/{self.purchase.pk}/",
            {**header, "line_id": str(self.line.pk), "line_product": self.item.sku, "line_quantity": "3", "line_unit_cost": "100", "line_supplier_sku": ""},
        )
        self.assertContains(lower, "5 already received, so the quantity can&#x27;t be lower.")
        other = product(self.organization, variant="100ML")
        swapped = self.client.post(
            f"/purchases/{self.purchase.pk}/",
            {**header, "line_id": str(self.line.pk), "line_product": other.sku, "line_quantity": "10", "line_unit_cost": "100", "line_supplier_sku": ""},
        )
        self.assertContains(swapped, "so the product stays")
        gone = self.client.post(
            f"/purchases/{self.purchase.pk}/",
            {**header, "line_id": "", "line_product": other.sku, "line_quantity": "1", "line_unit_cost": "1", "line_supplier_sku": ""},
        )
        self.assertContains(gone, "so the line stays")
        self.line.refresh_from_db()
        self.assertEqual((self.line.quantity, self.line.product), (10, self.item))


@override_settings(MELI_WEBHOOK_SECRET=WEBHOOK_SECRET, MELI_CLIENT_ID=str(SAMPLE["application_id"]))
class SalesTests(TestCase):
    def setUp(self):
        self.organization = shared_seller("Test Seller")
        self.item = product(self.organization, variant="30ML")
        self.single = listing(self.organization, "MLA1", self.item, available_quantity=10)
        self.started = timezone.now() - timedelta(hours=1)
        ledger.start(self.organization, at=self.started)
        ledger.count(self.organization, self.item, OWN, 10, opening=True)

    def test_a_paid_order_takes_units_once_and_a_cancel_puts_them_back(self):
        record_order(self.organization, order(2000001, "MLA1", 2))
        record_order(self.organization, order(2000001, "MLA1", 2))
        self.assertEqual(ledger.on_hand(self.item)[OWN], 8)
        sale = MeliSale.objects.get(order_id="2000001")
        self.assertTrue(sale.applied)
        record_order(self.organization, order(2000001, "MLA1", 2, status="cancelled"))
        record_order(self.organization, order(2000001, "MLA1", 2, status="cancelled"))
        self.assertEqual(ledger.on_hand(self.item)[OWN], 10)
        self.assertEqual(StockMovement.objects.filter(kind="sale_cancel").count(), 1)

    def test_unpaid_and_earlier_orders_leave_stock_alone(self):
        record_order(self.organization, order(2000002, "MLA1", 1, status="payment_required"))
        record_order(self.organization, order(2000003, "MLA1", 1, when=self.started - timedelta(days=1)))
        self.assertEqual(ledger.on_hand(self.item)[OWN], 10)
        record_order(self.organization, order(2000002, "MLA1", 1, status="paid"))
        self.assertEqual(ledger.on_hand(self.item)[OWN], 9)

    def test_full_sales_come_out_of_full_and_kits_take_more(self):
        ledger.send_to_full(self.organization, self.item, 4)
        listing(self.organization, "MLA2", self.item, logistic_type="fulfillment", inventory_id="INV1")
        listing(self.organization, "MLA3", self.item, units_per_sale=2, title="Kit x2")
        record_order(self.organization, order(2000004, "MLA2", 1))
        record_order(self.organization, order(2000005, "MLA3", 2))
        self.assertEqual(ledger.on_hand(self.item), {OWN: 6 - 4, FULL: 3})
        self.assertTrue(MeliSale.objects.get(order_id="2000004").from_full)

    def test_a_sale_waits_for_its_listing_link(self):
        listing(self.organization, "MLA9", seller_sku="PERF-ONLY-777-LATE")
        record_order(self.organization, order(2000006, "MLA9", 3))
        sale = MeliSale.objects.get(order_id="2000006")
        self.assertFalse(sale.applied)
        # Creating the catalog product links the listing, which takes the waiting sale.
        later = product(self.organization, variant="LATE", code="777")
        sale.refresh_from_db()
        self.assertTrue(sale.applied)
        self.assertEqual(ledger.on_hand(later)[OWN], -3)

    def test_an_orders_webhook_loads_the_order_for_each_started_organization(self):
        second = shared_seller("Second")
        theirs = product(second, variant="30ML")
        listing(second, "MLA1", theirs)
        stranger = Organization.objects.create(name="Stranger")
        product(stranger, variant="30ML")
        calls = []

        def api(request, timeout=15):
            calls.append(request.full_url)
            return TokenResponse(order(2000007, "MLA1", 1))

        body = json.dumps({**SAMPLE, "_id": "order-note", "topic": "orders_v2", "resource": "/orders/2000007"})
        with patch("meli.client.urlopen", side_effect=api):
            self.client.post(WEBHOOK_URL, data=body, content_type="application/json")
        self.assertEqual(len(calls), 1)
        self.assertEqual(ledger.on_hand(self.item)[OWN], 9)
        self.assertFalse(MeliSale.objects.filter(organization=second).exists())
        ledger.start(second)
        ledger.count(second, theirs, OWN, 5)
        with patch("meli.client.urlopen", side_effect=api):
            self.client.post(WEBHOOK_URL, data=body.replace("order-note", "order-note-2"), content_type="application/json")
        self.assertEqual(ledger.on_hand(theirs)[OWN], 4)
        self.assertEqual(ledger.on_hand(self.item)[OWN], 9)
        self.assertFalse(StockMovement.objects.filter(organization=stranger).exists())


class InventoryPageTests(TestCase):
    def setUp(self):
        self.organization = shared_seller("Test Seller")
        self.item = product(self.organization, variant="30ML")
        self.other = Organization.objects.create(name="Other")
        self.theirs = product(self.other, variant="SECRET")

    def test_without_catalog_access_inventory_is_closed(self):
        self.client.force_login(member(self.organization, apps=()))
        for url in ("/inventory/", "/inventory/count/", "/inventory/movements/", "/inventory/sales/", f"/inventory/products/{self.item.pk}/"):
            self.assertEqual(self.client.get(url).status_code, 403, url)

    def test_the_first_count_starts_inventory(self):
        self.client.force_login(member(self.organization))
        page = self.client.get("/inventory/count/")
        self.assertContains(page, "This is the first count.")
        self.assertNotContains(page, "SECRET")
        saved = self.client.post("/inventory/count/", {f"own_{self.item.pk}": "12", f"meli_full_{self.item.pk}": "0"})
        self.assertRedirects(saved, "/inventory/?counted=1")
        self.assertIsNotNone(InventorySettings.objects.get(organization=self.organization).started_at)
        self.assertEqual(StockMovement.objects.get().kind, "opening")
        stock = self.client.get("/inventory/")
        self.assertContains(stock, "Counting since")
        self.assertContains(stock, "PERF-ONLY-885-30ML")
        self.assertNotContains(stock, "SECRET")
        bad = self.client.post("/inventory/count/", {f"own_{self.item.pk}": "-1"})
        self.assertContains(bad, "enter a whole number of 0 or more")

    def test_the_stock_page_flags_a_difference_with_mercado_libre(self):
        self.client.force_login(member(self.organization))
        ledger.start(self.organization)
        ledger.count(self.organization, self.item, OWN, 10, opening=True)
        listing(self.organization, "MLA1", self.item, available_quantity=10)
        listing(self.organization, "MLA3", self.item, available_quantity=5, units_per_sale=2)
        self.assertNotContains(self.client.get("/inventory/"), "Differs")
        MeliListing.objects.filter(item_id="MLA1").update(available_quantity=7)
        flagged = self.client.get("/inventory/?show=differences")
        self.assertContains(flagged, "Differs")
        self.assertContains(flagged, "PERF-ONLY-885-30ML")

    def test_the_product_card_adjusts_counts_and_sends_to_full(self):
        self.client.force_login(member(self.organization))
        url = f"/inventory/products/{self.item.pk}/"
        self.assertContains(self.client.get(url), "Depósito")
        self.client.post(url, {"action": "count", "location": "own", "units": "8"})
        self.client.post(url, {"action": "full", "units": "3", "note": "Envío 123"})
        card = self.client.post(url, {"action": "adjust", "kind": "damage", "units": "1", "note": "Broken"})
        self.assertContains(card, "Stock updated.")
        self.assertEqual(ledger.on_hand(self.item), {OWN: 4, FULL: 3})
        bad = self.client.post(url, {"action": "full", "units": "0"})
        self.assertContains(bad, "Send at least one unit.")
        self.assertEqual(self.client.get(f"/inventory/products/{self.theirs.pk}/").status_code, 404)
        movements = self.client.get("/inventory/movements/?kind=transfer_in")
        self.assertContains(movements, "Arrived at Full")
        self.assertNotContains(movements, "Broken")
        self.assertContains(self.client.get(f"/catalog/products/{self.item.pk}/"), f'hx-get="/inventory/products/{self.item.pk}/"')

    def test_a_contributor_sees_stock_but_cannot_change_it(self):
        self.client.force_login(member(self.organization, role=Membership.Role.CONTRIBUTOR))
        self.assertEqual(self.client.get("/inventory/").status_code, 200)
        self.assertNotContains(self.client.get(f"/inventory/products/{self.item.pk}/"), "Send to Full")
        for url, data in (
            ("/inventory/count/", {f"own_{self.item.pk}": "5"}),
            (f"/inventory/products/{self.item.pk}/", {"action": "count", "units": "5"}),
            ("/inventory/sales/", {}),
        ):
            self.assertEqual(self.client.post(url, data).status_code, 403, url)
        self.assertFalse(StockMovement.objects.exists())

    def test_the_sales_page_explains_each_sale(self):
        self.client.force_login(member(self.organization))
        ledger.start(self.organization, at=timezone.now() - timedelta(hours=1))
        listing(self.organization, "MLA1", self.item)
        listing(self.organization, "MLA9")
        record_order(self.organization, order(2000010, "MLA1", 1))
        record_order(self.organization, order(2000011, "MLA9", 1))
        page = self.client.get("/inventory/sales/")
        self.assertContains(page, "Taken from stock")
        self.assertContains(page, "No catalog product")
        unlinked = self.client.get("/inventory/sales/?state=unlinked")
        self.assertContains(unlinked, "2000011")
        self.assertNotContains(unlinked, "2000010")


class MercadoLibreStockTests(TestCase):
    def setUp(self):
        self.organization = shared_seller("Test Seller")
        self.item = product(self.organization, variant="30ML")

    def test_full_stock_is_read_per_inventory_id(self):
        from meli.listings import refresh_full_stock

        listing(self.organization, "MLA2", self.item, logistic_type="fulfillment", inventory_id="INV1")
        listing(self.organization, "MLA4", self.item, logistic_type="fulfillment", inventory_id="INV1", variation_id="9")
        listing(self.organization, "MLA5", self.item)
        calls = []

        def api(request, timeout=15):
            calls.append(request.full_url)
            return TokenResponse({"available_quantity": 7, "not_available_quantity": 1})

        with patch("meli.client.urlopen", side_effect=api):
            self.assertEqual(refresh_full_stock(self.organization), 1)
        self.assertEqual(calls, ["https://api.mercadolibre.com/inventories/INV1/stock/fulfillment"])
        self.assertEqual(
            list(MeliListing.objects.filter(inventory_id="INV1").values_list("full_available", flat=True)), [7, 7]
        )
        self.assertIsNone(MeliListing.objects.get(item_id="MLA5").full_available)

    def test_owners_set_units_per_sale_and_the_return_stays_on_this_site(self):
        kit = listing(self.organization, "MLA3", self.item)
        self.client.force_login(member(self.organization))
        response = self.client.post(
            "/catalog/meli/",
            {"action": "units", "listing": kit.pk, "units_per_sale": "2", "next": "https://evil.example.com/"},
        )
        self.assertRedirects(response, "/catalog/meli/", fetch_redirect_response=False)
        kit.refresh_from_db()
        self.assertEqual(kit.units_per_sale, 2)
        self.client.post("/catalog/meli/", {"action": "units", "listing": kit.pk, "units_per_sale": "0"})
        kit.refresh_from_db()
        self.assertEqual(kit.units_per_sale, 2)
        theirs = listing(shared_seller("Other", user_id="555"), "MLA8")
        self.assertEqual(
            self.client.post("/catalog/meli/", {"action": "units", "listing": theirs.pk, "units_per_sale": "3"}).status_code,
            404,
        )
        self.client.force_login(member(self.organization, role=Membership.Role.CONTRIBUTOR, username="cara"))
        self.assertEqual(
            self.client.post("/catalog/meli/", {"action": "units", "listing": kit.pk, "units_per_sale": "5"}).status_code,
            403,
        )

    def test_the_sales_catch_up_reads_orders_since_the_first_count(self):
        from meli.sales import sync_sales

        listing(self.organization, "MLA1", self.item)
        refused = sync_sales(self.organization)
        self.assertEqual(refused.error, "Count the stock first.")
        started = timezone.now() - timedelta(hours=2)
        ledger.start(self.organization, at=started)
        ledger.count(self.organization, self.item, OWN, 10, opening=True)
        asked = []

        def api(request, timeout=15):
            asked.append(request.full_url)
            return TokenResponse(
                {"results": [order(2000020, "MLA1", 1), order(2000021, "MLA1", 2)], "paging": {"total": 2}}
            )

        with patch("meli.client.urlopen", side_effect=api):
            record = sync_sales(self.organization)
        self.assertEqual((record.state, record.orders), ("ok", 2))
        self.assertIn("sort=date_asc", asked[0])
        self.assertEqual(ledger.on_hand(self.item)[OWN], 7)
