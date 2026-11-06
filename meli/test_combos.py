from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from catalog.models import ComboItem
from catalog.test_combos import combo
from catalog.tests import product
from inventory import ledger
from inventory.ledger import OWN
from inventory.models import StockMovement
from inventory.tests import listing, order

from .drafts import build_item, defaults_for
from .listings import store_items
from .models import ListingDraft, MeliListing, MeliSale
from .sales import record_order
from .test_drafts import DraftTestCase
from .test_listings import item
from .test_product_drafts import PERFUME_ATTRIBUTES


class ComboStockTests(DraftTestCase):
    """A combo's listing links by its SKU, and each sale takes every component."""

    def setUp(self):
        super().setUp()
        self.other = product(self.organization, variant="100ML")
        ledger.record(self.organization, self.other, ledger.Kind.OPENING, OWN, 5)
        self.combo = combo(self.organization, code="885MIX", title="Kit 885 30 ml + 100 ml", list_price=Decimal("45000"))
        ComboItem.objects.create(combo=self.combo, product=self.product, quantity=2)
        ComboItem.objects.create(combo=self.combo, product=self.other, quantity=1)

    def test_the_sync_links_a_combo_sku_and_a_new_combo_takes_its_listings(self):
        store_items(self.organization, {"MLA700": item("MLA700", sku="combo-885mix")})
        listing_row = MeliListing.objects.get(item_id="MLA700")
        self.assertEqual((listing_row.product, listing_row.combo), (None, self.combo))
        store_items(self.organization, {"MLA701": item("MLA701", sku="COMBO-LATER")})
        with self.captureOnCommitCallbacks(execute=True):
            later = combo(self.organization, code="LATER", title="Pack later")
        self.assertEqual(MeliListing.objects.get(item_id="MLA701").combo, later)

    def test_a_combo_sale_takes_each_component_once_and_a_cancel_puts_them_back(self):
        listing(self.organization, "MLA700", combo=self.combo, seller_sku=self.combo.sku)
        record_order(self.organization, order(3000001, "MLA700", 1))
        record_order(self.organization, order(3000001, "MLA700", 1))
        self.assertEqual((ledger.on_hand(self.product)[OWN], ledger.on_hand(self.other)[OWN]), (3 - 2, 5 - 1))
        self.assertTrue(MeliSale.objects.get(order_id="3000001").applied)
        record_order(self.organization, order(3000001, "MLA700", 1, status="cancelled"))
        record_order(self.organization, order(3000001, "MLA700", 1, status="cancelled"))
        self.assertEqual((ledger.on_hand(self.product)[OWN], ledger.on_hand(self.other)[OWN]), (3, 5))
        keys = sorted(StockMovement.objects.filter(meli_sale__order_id="3000001").values_list("source_key", flat=True))
        self.assertEqual(len(keys), 4)
        self.assertTrue(all(key.endswith((str(self.product.pk), str(self.other.pk))) for key in keys))

    def test_a_waiting_sale_is_taken_once_the_combo_and_its_lines_are_saved(self):
        listing(self.organization, "MLA702", seller_sku="COMBO-WAIT")
        record_order(self.organization, order(3000002, "MLA702", 1))
        self.assertFalse(MeliSale.objects.get(order_id="3000002").applied)
        # The combo page saves the combo and its lines together; the link waits for the commit.
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                "/catalog/combos/new/",
                {"code": "WAIT", "title": "Combo wait", "image_url": "https://img.example/w.jpg", "status": "active",
                 "currency": "ARS", "combo_sku": [self.other.sku], "combo_quantity": ["2"]},
            )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(MeliSale.objects.get(order_id="3000002").applied)
        self.assertEqual(ledger.on_hand(self.other)[OWN], 5 - 2)


class ComboDraftTests(DraftTestCase):
    def setUp(self):
        super().setUp()
        self.meli.category_attributes = PERFUME_ATTRIBUTES
        self.other = product(self.organization, variant="100ML")
        ledger.record(self.organization, self.other, ledger.Kind.OPENING, OWN, 5)
        self.combo = combo(
            self.organization, code="885MIX", title="Kit 885 30 ml + 100 ml", list_price=Decimal("45000"),
            description="Dos perfumes 885.", meli_category_id="MLA1271", weight_g=400,
            height_cm=Decimal("18"), width_cm=Decimal("12"), length_cm=Decimal("8"),
        )
        ComboItem.objects.create(combo=self.combo, product=self.product, quantity=1)
        ComboItem.objects.create(combo=self.combo, product=self.other, quantity=1)

    def create(self):
        return self.client.post(f"/catalog/meli/combos/{self.combo.pk}/draft/")

    def test_the_combo_page_offers_the_button_while_it_can_be_made(self):
        self.assertContains(self.client.get(f"/catalog/meli/combos/{self.combo.pk}/"), "Create Mercado Libre listing")
        ledger.record(self.organization, self.other, ledger.Kind.DAMAGE, OWN, -5)
        actions = self.client.get(f"/catalog/meli/combos/{self.combo.pk}/")
        self.assertContains(actions, "Not enough stock in the Depósito to make one.")
        self.assertEqual(self.create().status_code, 400)

    def test_the_body_sells_the_combo_sku_as_brand_generic(self):
        response = self.create()
        draft = ListingDraft.objects.get(combo=self.combo)
        self.assertRedirects(response, f"/catalog/meli/new/{draft.pk}/")
        self.assertEqual((draft.kind, draft.product), (ListingDraft.Kind.COMBO, None))
        body = build_item(draft, defaults_for(self.organization))
        sent = {attribute["id"]: attribute.get("value_name") or attribute.get("value_id") for attribute in body["attributes"]}
        self.assertEqual(sent["SELLER_SKU"], "COMBO-885MIX")
        self.assertEqual(sent["BRAND"], "35977846")
        self.assertEqual(sent["MODEL"], "Kit 885 30 ml + 100 ml")
        self.assertEqual(sent["SELLER_PACKAGE_WEIGHT"], "400 g")
        self.assertEqual(body["title"], "Kit 885 30 ml + 100 ml")
        self.assertEqual(body["pictures"], [{"source": self.combo.image_url}])
        page = self.client.get(f"/catalog/meli/new/{draft.pk}/")
        self.assertContains(page, "Combo listing of")
        self.assertContains(page, "COMBO-885MIX")

    def test_quantity_is_capped_by_how_many_combos_stock_makes_and_posting_adds_the_description(self):
        self.create()
        draft = ListingDraft.objects.get(combo=self.combo)
        response = self.client.post(
            f"/catalog/meli/new/{draft.pk}/",
            {
                "price": "45000", "quantity": "4", "category_id": "MLA1271", "use_defaults": "on",
                "condition": "new", "warranty": "seller", "warranty_days": "30",
                "attr_key": ["PERFUME_TYPE", "UNIT_VOLUME"], "attr_value": ["Eau de parfum", "130 mL"],
            },
        )
        self.assertContains(response, "Only 3 in stock in the Depósito.")
        self.client.post(
            f"/catalog/meli/new/{draft.pk}/",
            {
                "price": "45000", "quantity": "3", "category_id": "MLA1271", "use_defaults": "on",
                "condition": "new", "warranty": "seller", "warranty_days": "30",
                "attr_key": ["PERFUME_TYPE", "UNIT_VOLUME"], "attr_value": ["Eau de parfum", "130 mL"],
            },
        )
        draft.refresh_from_db()
        self.assertEqual(draft.status, ListingDraft.Status.READY, draft.error)
        self.client.post("/catalog/meli/new/", {"action": "post_all"})
        draft.refresh_from_db()
        self.assertEqual(draft.status, ListingDraft.Status.PUBLISHED, draft.error)
        self.assertEqual(self.meli.descriptions, [{"plain_text": "Dos perfumes 885."}])
        self.assertEqual(MeliListing.objects.get(item_id="MLA900").combo, self.combo)
        self.assertContains(self.client.get("/catalog/meli/new/"), "· combo")
