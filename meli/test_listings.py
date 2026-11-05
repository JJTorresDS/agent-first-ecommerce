import json
from decimal import Decimal
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse

from django.test import TestCase, override_settings

from accounts.models import AppAccess, Membership, Organization
from catalog.test_views import member
from catalog.tests import product

from .listings import rows_for, sku_of, sync_listings
from .models import MeliListing, MeliListingSync
from .tests import SAMPLE, WEBHOOK_SECRET, WEBHOOK_URL, TokenResponse, shared_seller

SELLER = "468424240"


def item(item_id, sku="", status="active", price=45000, variations=None, **fields):
    body = {
        "id": item_id,
        "title": fields.pop("title", f"Listing {item_id}"),
        "status": status,
        "price": price,
        "currency_id": "ARS",
        "available_quantity": fields.pop("available_quantity", 5),
        "sold_quantity": 2,
        "listing_type_id": "gold_special",
        "category_id": "MLA1271",
        "condition": "new",
        "permalink": f"https://articulo.mercadolibre.com.ar/{item_id}",
        "secure_thumbnail": f"https://http2.mlstatic.com/{item_id}.jpg",
        "date_created": "2026-10-01T12:00:00.000Z",
        "last_updated": "2026-11-01T12:00:00.000Z",
        "attributes": [{"id": "SELLER_SKU", "value_name": sku}] if sku else [],
        **fields,
    }
    if variations is not None:
        body["variations"] = variations
    return body


def variation(variation_id, sku, label, price=30000, available=3):
    return {
        "id": variation_id,
        "price": price,
        "available_quantity": available,
        "sold_quantity": 1,
        "attribute_combinations": [{"name": "Volumen", "value_name": label}],
        "attributes": [{"id": "SELLER_SKU", "value_name": sku}],
    }


class FakeMeli:
    """Answers the scan search and the multi-get like Mercado Libre, and records each call."""

    def __init__(self, *items, page=2):
        self.items = {body["id"]: body for body in items}
        self.page = page
        self.calls = []
        self.fail_search = False

    def __call__(self, request, timeout=15):
        url = urlparse(request.full_url)
        query = parse_qs(url.query)
        self.calls.append(request.full_url)
        if url.path == f"/users/{SELLER}/items/search":
            if self.fail_search:
                raise URLError("down")
            assert query["search_type"] == ["scan"], query
            ids = [key for key, body in self.items.items() if body["status"] == query["status"][0]]
            start = int((query.get("scroll_id") or ["s0"])[0][1:])
            chunk = ids[start : start + self.page]
            return TokenResponse({"results": chunk, "scroll_id": f"s{start + self.page}"})
        if url.path == "/items":
            assert query["include_attributes"] == ["all"], query
            asked = query["ids"][0].split(",")
            assert len(asked) <= 20, asked
            return TokenResponse(
                [
                    {"code": 200, "body": self.items[key]}
                    if key in self.items
                    else {"code": 404, "body": {"message": f"Item with id {key} not found", "error": "not_found"}}
                    for key in asked
                ]
            )
        raise AssertionError(request.full_url)


class SkuTests(TestCase):
    def test_the_sku_comes_from_the_attribute_then_the_custom_field_variation_first(self):
        self.assertEqual(sku_of(item("MLA1", sku=" PERF-ONLY-885-30ML ")), "PERF-ONLY-885-30ML")
        self.assertEqual(sku_of({"seller_custom_field": "legacy-1", "attributes": []}), "legacy-1")
        self.assertEqual(sku_of({"attributes": []}), "")
        both = variation(11, "PERF-ONLY-885-30ML", "30 ml")
        self.assertEqual(sku_of(item("MLA2", sku="ITEM-LEVEL"), both), "PERF-ONLY-885-30ML")
        self.assertEqual(sku_of(item("MLA2"), {"seller_custom_field": "VAR-CUSTOM"}), "VAR-CUSTOM")

    def test_an_item_with_variations_becomes_one_row_each(self):
        rows = rows_for(
            item(
                "MLA2",
                variations=[variation(11, "A-30", "30 ml", 30000, 3), variation(12, "A-100", "100 ml", 90000, 1)],
            )
        )
        self.assertEqual([(row["variation_id"], row["seller_sku"], row["variation_label"]) for row in rows],
                         [("11", "A-30", "Volumen: 30 ml"), ("12", "A-100", "Volumen: 100 ml")])
        self.assertEqual(rows[1]["price"], Decimal("90000.00"))
        self.assertEqual(rows[1]["available_quantity"], 1)
        self.assertNotIn("variations", rows[0]["payload"])
        self.assertEqual(rows[0]["payload"]["variation"]["id"], 11)


@override_settings(MELI_WEBHOOK_SECRET=WEBHOOK_SECRET, MELI_CLIENT_ID=str(SAMPLE["application_id"]))
class ListingSyncTests(TestCase):
    def setUp(self):
        self.organization = shared_seller("Test Seller")
        self.small = product(self.organization, variant="30ML")
        self.large = product(self.organization, variant="100ML")
        self.meli = FakeMeli(
            item("MLA1", sku="perf-only-885-30ml"),
            item("MLA2", sku="PERF-ONLY-885-30ML", status="paused", title="Kit"),
            item("MLA3", status="closed", variations=[variation(31, "PERF-ONLY-885-100ML", "100 ml"), variation(32, "", "50 ml")]),
            item("MLA4", sku="NOT-IN-VIMPLE", status="under_review"),
            item("MLA5"),
        )

    def _sync(self):
        with patch("meli.client.urlopen", side_effect=self.meli):
            return sync_listings(self.organization)

    def test_a_full_sync_reads_every_status_and_links_by_sku(self):
        record = self._sync()
        self.assertEqual(record.state, MeliListingSync.State.OK)
        searched = {parse_qs(urlparse(url).query)["status"][0] for url in self.meli.calls if "/items/search" in url}
        self.assertEqual(searched, {"active", "paused", "closed", "under_review", "inactive"})
        self.assertTrue(any("scroll_id=s2" in url for url in self.meli.calls))
        rows = {(row.item_id, row.variation_id): row for row in MeliListing.objects.filter(organization=self.organization)}
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[("MLA1", "")].product, self.small)
        self.assertEqual(rows[("MLA2", "")].product, self.small)
        self.assertEqual(rows[("MLA3", "31")].product, self.large)
        self.assertIsNone(rows[("MLA3", "32")].product)
        self.assertIsNone(rows[("MLA4", "")].product)
        self.assertEqual(sorted(self.small.meli_listings.values_list("item_id", flat=True)), ["MLA1", "MLA2"])
        self.assertEqual(rows[("MLA1", "")].seller_sku, "perf-only-885-30ml")
        self.assertEqual((record.total, record.linked, record.unmatched, record.no_sku), (6, 3, 1, 2))

    def test_a_re_sync_updates_and_removes_vanished_items(self):
        self._sync()
        self.meli.items["MLA1"]["price"] = 47000
        self.meli.items["MLA1"]["available_quantity"] = 9
        del self.meli.items["MLA4"]
        self.meli.items["MLA3"]["variations"] = self.meli.items["MLA3"]["variations"][:1]
        self._sync()
        first = MeliListing.objects.get(organization=self.organization, item_id="MLA1")
        self.assertEqual((first.price, first.available_quantity), (Decimal("47000.00"), 9))
        self.assertFalse(MeliListing.objects.filter(item_id="MLA4").exists())
        self.assertEqual(list(MeliListing.objects.filter(item_id="MLA3").values_list("variation_id", flat=True)), ["31"])

    def test_a_failed_search_keeps_every_row_and_says_why(self):
        self._sync()
        self.meli.fail_search = True
        with self.assertLogs("meli", "WARNING") as logs:
            record = self._sync()
        self.assertEqual(record.state, MeliListingSync.State.FAILED)
        self.assertIn("could not be reached", record.error)
        self.assertIn("Listing sync failed", "\n".join(logs.output))
        self.assertEqual(MeliListing.objects.filter(organization=self.organization).count(), 6)

    def test_an_items_webhook_refreshes_just_that_item(self):
        self._sync()
        self.meli.items["MLA1"]["price"] = 50000
        self.meli.items["MLA2"]["price"] = 1
        self.meli.calls.clear()
        with patch("meli.client.urlopen", side_effect=self.meli):
            response = self.client.post(
                WEBHOOK_URL,
                data=json.dumps({**SAMPLE, "_id": "item-note", "topic": "items", "resource": "/items/MLA1"}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.meli.calls), 1)
        self.assertEqual(MeliListing.objects.get(item_id="MLA1").price, Decimal("50000.00"))
        self.assertEqual(MeliListing.objects.get(item_id="MLA2").price, Decimal("45000.00"))
        del self.meli.items["MLA1"]
        with patch("meli.client.urlopen", side_effect=self.meli):
            self.client.post(
                WEBHOOK_URL,
                data=json.dumps({**SAMPLE, "_id": "item-gone", "topic": "items", "resource": "/items/MLA1"}),
                content_type="application/json",
            )
        self.assertFalse(MeliListing.objects.filter(item_id="MLA1").exists())

    def test_a_catalog_product_created_later_takes_its_listings(self):
        self._sync()
        MeliListing.objects.filter(item_id="MLA4").update(seller_sku="perf-only-777-late")
        MeliListing.objects.filter(item_id="MLA1").update(seller_sku="PERF-ONLY-777-LATE")
        later = product(self.organization, variant="LATE", code="777")
        self.assertEqual(MeliListing.objects.get(item_id="MLA4").product, later)
        # A listing already linked to another product keeps its link.
        self.assertEqual(MeliListing.objects.get(item_id="MLA1").product, self.small)

    def test_organizations_sharing_a_seller_link_only_their_own_products(self):
        second = shared_seller("Second")
        theirs = product(second, variant="30ML")
        stranger = Organization.objects.create(name="Stranger")
        product(stranger, variant="30ML")
        self._sync()
        with patch("meli.client.urlopen", side_effect=self.meli):
            sync_listings(second)
        mine = MeliListing.objects.get(organization=self.organization, item_id="MLA1")
        copy = MeliListing.objects.get(organization=second, item_id="MLA1")
        self.assertEqual((mine.product, copy.product), (self.small, theirs))
        self.assertFalse(MeliListing.objects.filter(organization=stranger).exists())
        self.assertFalse(MeliListing.objects.filter(product__organization=stranger).exists())


@override_settings(MELI_WEBHOOK_SECRET=WEBHOOK_SECRET, MELI_CLIENT_ID=str(SAMPLE["application_id"]))
class ListingPageTests(TestCase):
    def setUp(self):
        self.organization = shared_seller("Test Seller")
        self.small = product(self.organization, variant="30ML")
        self.meli = FakeMeli(item("MLA1", sku="PERF-ONLY-885-30ML", title="Only 885 30 ml"), item("MLA4", title="Sin SKU"))

    def test_without_catalog_access_the_page_is_closed(self):
        self.client.force_login(member(self.organization, apps=()))
        self.assertEqual(self.client.get("/catalog/meli/").status_code, 403)

    def test_an_owner_syncs_and_sees_the_links(self):
        self.client.force_login(member(self.organization))
        first = self.client.get("/catalog/meli/")
        self.assertContains(first, "Not synced yet.")
        self.assertContains(first, "Sync listings")
        with patch("meli.client.urlopen", side_effect=self.meli):
            response = self.client.post("/catalog/meli/")
        self.assertRedirects(response, "/catalog/meli/")
        page = self.client.get("/catalog/meli/")
        self.assertContains(page, "2 listings")
        self.assertContains(page, "<strong>1</strong> linked")
        self.assertContains(page, f'href="/catalog/products/{self.small.pk}/"')
        self.assertContains(page, "No SKU")
        linked = self.client.get("/catalog/meli/?link=linked", HTTP_HX_REQUEST="true")
        self.assertContains(linked, "Only 885 30 ml")
        self.assertNotContains(linked, "Sin SKU")
        missing = self.client.get("/catalog/meli/?link=no_sku")
        self.assertContains(missing, "Sin SKU")
        card = self.client.get(f"/catalog/products/{self.small.pk}/")
        self.assertContains(card, "Mercado Libre listings")
        self.assertContains(card, "MLA1")

    def test_the_status_poll_reloads_when_the_sync_ends(self):
        self.client.force_login(member(self.organization))
        MeliListingSync.objects.create(organization=self.organization, state=MeliListingSync.State.RUNNING)
        running = self.client.get("/catalog/meli/?poll=sync", HTTP_HX_REQUEST="true")
        self.assertContains(running, "Reading your Mercado Libre listings")
        self.assertNotIn("HX-Refresh", running.headers)
        MeliListingSync.objects.filter(organization=self.organization).update(state=MeliListingSync.State.OK)
        done = self.client.get("/catalog/meli/?poll=sync", HTTP_HX_REQUEST="true")
        self.assertEqual(done.headers.get("HX-Refresh"), "true")

    def test_a_contributor_sees_listings_but_cannot_sync(self):
        with patch("meli.client.urlopen", side_effect=self.meli):
            sync_listings(self.organization)
        self.client.force_login(member(self.organization, role=Membership.Role.CONTRIBUTOR))
        page = self.client.get("/catalog/meli/")
        self.assertContains(page, "MLA1")
        self.assertNotContains(page, "Sync listings")
        self.assertEqual(self.client.post("/catalog/meli/").status_code, 403)

    def test_another_organizations_listings_stay_out(self):
        other = shared_seller("Other", user_id="555")
        MeliListing.objects.create(organization=other, item_id="MLA999", title="Their listing", synced_at=self.small.created_at)
        self.client.force_login(member(self.organization))
        page = self.client.get("/catalog/meli/?q=MLA")
        self.assertNotContains(page, "Their listing")
        self.assertNotContains(page, "MLA999")

    def test_without_mercado_libre_the_page_says_so(self):
        organization = Organization.objects.create(name="Unconnected")
        self.client.force_login(member(organization, username="bea"))
        page = self.client.get("/catalog/meli/")
        self.assertContains(page, "Mercado Libre isn't connected")
        self.assertEqual(self.client.post("/catalog/meli/").status_code, 302)
        self.assertFalse(MeliListingSync.objects.filter(organization=organization).exists())
