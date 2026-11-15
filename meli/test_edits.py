import json
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlparse

from django.test import TestCase

from accounts.models import Membership
from catalog.test_views import member
from catalog.tests import product
from inventory import ledger
from inventory.tests import listing

from .listing_edits import read_price
from .models import ListingChange, MeliListing
from .tests import TokenResponse, shared_seller


class ListingEditTests(TestCase):
    """Price and available quantity change on Mercado Libre from the Listings page, with a reason, in the worker."""

    def setUp(self):
        self.organization = shared_seller("Molihaus")
        self.client.force_login(member(self.organization))
        self.puts = []
        self.refuse = ""
        self.item = product(self.organization, variant="30ML")
        self.single = listing(
            self.organization, "MLA1", self.item, title="Perfume A", status="active", price=Decimal("1000"),
            available_quantity=3, currency="ARS",
        )
        self.variant = listing(self.organization, "MLA2", variation_id="21", title="Perfume B", status="active",
                               price=Decimal("500"), available_quantity=1)
        listing(self.organization, "MLA2", variation_id="22", title="Perfume B", status="active",
                price=Decimal("500"), available_quantity=4)
        patcher = patch("meli.client.urlopen", side_effect=self.api)
        patcher.start()
        self.addCleanup(patcher.stop)

    def api(self, request, timeout=15):
        url = urlparse(request.full_url)
        if request.get_method() == "PUT":
            if self.refuse:
                body = json.dumps({"message": self.refuse, "error": "validation_error", "status": 400})
                raise HTTPError(request.full_url, 400, "Bad Request", {}, BytesIO(body.encode()))
            self.puts.append((url.path.split("/")[2], json.loads(request.data)))
            return TokenResponse({"id": "x"})
        return TokenResponse([])

    def edit(self, row, field, value, reason="price_review", note=""):
        return self.client.post("/catalog/meli/", {
            "action": "edit", "listing": row.pk, "field": field, "value": value, "reason": reason, "note": note,
            "next": "/catalog/meli/?q=Perfume",
        }, follow=True)

    def test_price_is_sent_logged_and_shown(self):
        response = self.edit(self.single, "price", "1.250,50", note="Supplier raised it")
        self.assertEqual(self.puts, [("MLA1", {"price": 1250.5})])
        self.assertEqual(MeliListing.objects.get(pk=self.single.pk).price, Decimal("1250.50"))
        change = ListingChange.objects.get()
        self.assertEqual((change.field, change.old_value, change.new_value, change.state),
                         ("price", "1000.00", "1250.50", "done"))
        self.assertEqual(response.redirect_chain[-1][0], "/catalog/meli/?q=Perfume")
        self.assertContains(response, "Price 1000.00 → 1250.50 by ada · Price review")

    def test_variations_are_all_named_and_quantity_goes_on_one(self):
        self.edit(self.variant, "available_quantity", "2", reason="out_of_stock")
        self.assertEqual(self.puts, [("MLA2", {"variations": [{"id": 21, "available_quantity": 2}, {"id": 22}]})])
        self.assertEqual(sorted(MeliListing.objects.filter(item_id="MLA2").values_list("available_quantity", flat=True)), [2, 4])
        self.edit(self.variant, "price", "600")
        self.assertEqual(self.puts[-1], ("MLA2", {"variations": [{"id": 21, "price": 600.0}, {"id": 22, "price": 600.0}]}))

    def test_quantity_is_capped_by_the_deposito_once_inventory_started(self):
        ledger.start(self.organization)
        ledger.record(self.organization, self.item, ledger.Kind.OPENING, ledger.OWN, 5)
        self.assertContains(self.client.get("/catalog/meli/"), 'data-max="5"')
        response = self.edit(self.single, "available_quantity", "6", reason="seasonal")
        self.assertContains(response, "Only 5 in the Depósito")
        self.assertEqual(self.puts, [])
        self.assertFalse(ListingChange.objects.exists())
        self.edit(self.single, "available_quantity", "5", reason="seasonal")
        self.assertEqual(self.puts, [("MLA1", {"available_quantity": 5})])

    def test_full_and_closed_listings_are_refused(self):
        MeliListing.objects.filter(pk=self.single.pk).update(logistic_type="fulfillment")
        page = self.client.get("/catalog/meli/")
        self.assertNotContains(page, f'data-field="available_quantity" data-listing="{self.single.pk}"')
        self.assertContains(self.edit(self.single, "available_quantity", "1"), "in Full")
        MeliListing.objects.filter(pk=self.single.pk).update(status="closed")
        self.assertContains(self.edit(self.single, "price", "900"), "closed listing")
        self.assertEqual(self.puts, [])

    def test_bad_values_and_missing_reasons_change_nothing(self):
        for value, reason, note, text in [
            ("0", "price_review", "", "Type a price above zero"),
            ("abc", "price_review", "", "Type a price above zero"),
            ("1000", "price_review", "", "already its price"),
            ("900", "", "", "Pick a reason first"),
            ("900", "other", " ", "Say why in the note"),
        ]:
            self.assertContains(self.edit(self.single, "price", value, reason, note), text)
        self.assertContains(self.edit(self.single, "available_quantity", "-1"), "whole number")
        self.assertEqual(self.puts, [])
        self.assertFalse(ListingChange.objects.exists())

    def test_a_refusal_shows_on_the_row(self):
        self.refuse = "The price is too low for this category"
        response = self.edit(self.single, "price", "1")
        change = ListingChange.objects.get()
        self.assertEqual(change.state, "failed")
        self.assertEqual(change.error, "Couldn't set the price: The price is too low for this category")
        self.assertContains(response, "The price is too low for this category")
        self.assertEqual(MeliListing.objects.get(pk=self.single.pk).price, Decimal("1000"))

    def test_the_row_polls_alone_while_queued(self):
        ListingChange.objects.create(organization=self.organization, item_id="MLA1", field="price",
                                     old_value="1000", new_value="1200", reason="seasonal")
        page = self.client.get("/catalog/meli/")
        self.assertContains(page, f'hx-get="/catalog/meli/row/{self.single.pk}/"', count=1)
        self.assertContains(page, "Setting price to 1200…")
        url = f"/catalog/meli/row/{self.single.pk}/"
        self.assertEqual(self.client.get(url).status_code, 204)
        ListingChange.objects.update(state="done")
        row = self.client.get(url, HTTP_HX_CURRENT_URL="http://testserver/catalog/meli/?status=active")
        self.assertContains(row, f'id="listing-row-{self.single.pk}"')
        self.assertContains(row, 'value="/catalog/meli/?status=active"')
        self.assertContains(row, f'value="{self.single.pk}" form="bulk-listings"')
        self.assertNotContains(row, "every 2s")

    def test_catalog_column(self):
        MeliListing.objects.filter(pk=self.single.pk).update(
            catalog_listing=True, payload={"catalog_product_id": "MLA45601658"}
        )
        MeliListing.objects.filter(item_id="MLA2").update(payload={"tags": ["catalog_listing_eligible"]})
        twin = listing(self.organization, "MLA3", title="Perfume C", status="active",
                       payload={"item_relations": [{"id": "MLA9", "stock_relation": 1}]})
        page = self.client.get("/catalog/meli/")
        self.assertContains(page, "Competing")
        self.assertContains(page, "https://www.mercadolibre.com.ar/p/MLA45601658")
        self.assertContains(page, "Can join", count=2)
        self.assertContains(page, "Via MLA9")
        self.assertEqual(twin.catalog_state(), "twin")

    def test_only_editors_and_only_their_listings(self):
        foreign = listing(shared_seller("Other"), "MLA9", title="Theirs", status="active", price=Decimal("10"))
        self.assertEqual(self.edit(foreign, "price", "20").status_code, 404)
        self.assertEqual(self.client.get(f"/catalog/meli/row/{foreign.pk}/").status_code, 404)
        self.client.force_login(member(self.organization, role=Membership.Role.CONTRIBUTOR, username="cid"))
        self.assertEqual(self.client.post("/catalog/meli/", {
            "action": "edit", "listing": self.single.pk, "field": "price", "value": "5", "reason": "seasonal",
        }).status_code, 403)
        self.assertNotContains(self.client.get("/catalog/meli/"), "data-edit-dialog data-field")
        self.assertEqual(self.puts, [])
        self.assertFalse(ListingChange.objects.exists())

    def test_read_price(self):
        self.assertEqual(read_price("1.234,5"), Decimal("1234.50"))
        self.assertEqual(read_price("1234.5"), Decimal("1234.50"))
        self.assertIsNone(read_price("1.2345"))
        self.assertIsNone(read_price("NaN"))
