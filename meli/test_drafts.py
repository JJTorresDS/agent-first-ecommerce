import json
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlparse

from django.test import TestCase
from django.utils import timezone

from accounts.models import AppAccess, Membership, Organization
from catalog.test_views import member
from catalog.tests import product
from inventory import ledger

from .drafts import DraftError, build_item, catalog_product_id, defaults_for, settings_of
from .models import ListingDraft, MeliListing
from .test_listings import item
from .tests import TokenResponse, shared_seller

CATALOG = "MLA45601658"
URL = f"https://www.mercadolibre.com.ar/caramel-dream-75-ml/p/{CATALOG}#reco"


class FakeSellFlow:
    """Answers the calls a draft makes, like Mercado Libre, and keeps every body it was sent."""

    def __init__(self):
        self.calls = []
        self.validated = []
        self.created = []
        self.refuse = ""
        self.refuse_text = ""
        self.warn_only = False
        self.product_status = "active"
        self.flex_removed = []
        self.category_attributes = [{"id": "BRAND"}, {"id": "155_98_RESOLUTION_FILE_NUMBER"}]
        self.categories_read = []
        self.descriptions = []
        self.refuse_description = False
        self.paused = []
        self.refuse_pause = False

    def _refused(self, request):
        if self.refuse_text:
            return HTTPError(request.full_url, 502, "Bad Gateway", {}, BytesIO(self.refuse_text.encode()))
        if self.warn_only:
            body = json.dumps(
                {
                    "error": "validation_error",
                    "status": 400,
                    "message": "Validation error",
                    "cause": [
                        {"code": "shipping.lost_me1_by_user", "type": "warning", "message": "User has not mode me1"},
                        {"code": "shipping.free_shipping.cost_exceeded", "type": "warning", "message": "Free shipping costs exceeds sale"},
                    ],
                }
            )
            return HTTPError(request.full_url, 400, "Bad Request", {}, BytesIO(body.encode()))
        body = json.dumps(
            {
                "message": "Validation error",
                "error": "validation_error",
                "status": 400,
                "cause": [
                    {"code": "item.title.length", "message": "A warning first", "type": "warning"},
                    {"code": "item.shipping.mode.invalid", "message": self.refuse, "type": "error", "references": ["item.shipping.mode"]},
                ],
            }
        )
        return HTTPError(request.full_url, 400, "Bad Request", {}, BytesIO(body.encode()))

    def __call__(self, request, timeout=15):
        url = urlparse(request.full_url)
        method = request.get_method()
        self.calls.append((method, url.path))
        body = json.loads(request.data.decode()) if request.data else None
        if url.path == f"/products/{CATALOG}":
            return TokenResponse(
                {
                    "id": CATALOG,
                    "name": "Caramel Dream Sweet Tooth 75 Ml",
                    "status": self.product_status,
                    "domain_id": "MLA-PERFUMES",
                    "pictures": [{"url": "https://http2.mlstatic.com/caramel.jpg"}],
                }
            )
        if url.path == "/sites/MLA/domain_discovery/search":
            return TokenResponse(
                [
                    {"domain_id": "MLA-COLOGNES", "category_id": "MLA9999"},
                    {"domain_id": "MLA-PERFUMES", "category_id": "MLA1271"},
                ]
            )
        if url.path.startswith("/categories/") and url.path.endswith("/attributes"):
            self.categories_read.append(url.path.split("/")[2])
            return TokenResponse(self.category_attributes)
        if url.path == "/items/MLA900/description" and method == "POST":
            self.descriptions.append(body)
            if self.refuse_description:
                raise self._refused(request)
            return TokenResponse({"text": "", "plain_text": body["plain_text"]})
        if url.path == "/items/validate" and method == "POST":
            self.validated.append(body)
            if self.refuse or self.warn_only:
                raise self._refused(request)
            return TokenResponse(None)
        if url.path == "/items" and method == "POST":
            if self.refuse:
                raise self._refused(request)
            self.created.append(body)
            return TokenResponse({"id": "MLA900", "shipping": {"tags": ["self_service_in"]}})
        if url.path == "/sites/MLA/shipping/selfservice/items/MLA900" and method == "DELETE":
            self.flex_removed.append("MLA900")
            return TokenResponse({})
        if url.path.startswith("/items/") and method == "PUT" and body == {"status": "paused"}:
            if self.refuse_pause:
                self.refuse = self.refuse or "Item can't be paused"
                raise self._refused(request)
            self.paused.append(url.path.split("/")[2])
            return TokenResponse({"id": url.path.split("/")[2], "status": "paused"})
        if url.path == "/items" and method == "GET":
            sku = self.created[-1]["attributes"][0]["value_name"]
            status = "paused" if "MLA900" in self.paused else "active"
            return TokenResponse([{"code": 200, "body": item("MLA900", sku=sku, status=status, catalog_listing=True)}])
        raise AssertionError(f"{method} {request.full_url}")


class CatalogUrlTests(TestCase):
    def test_a_catalog_page_gives_its_product_id(self):
        self.assertEqual(catalog_product_id(URL), CATALOG)
        self.assertEqual(catalog_product_id("https://www.mercadolibre.com.ar/p/mla45601658?pdp_filters=x"), CATALOG)

    def test_a_listing_url_or_junk_is_refused_with_what_to_paste(self):
        with self.assertRaisesMessage(DraftError, "Vender uno igual"):
            catalog_product_id("https://articulo.mercadolibre.com.ar/MLA-4160390910-caramel-_JM")
        with self.assertRaises(DraftError):
            catalog_product_id("https://example.com/perfume")


class DraftTestCase(TestCase):
    def setUp(self):
        self.organization = shared_seller("Molihaus")
        self.owner = member(self.organization)
        self.client.force_login(self.owner)
        self.product = product(self.organization, variant="75ML")
        ledger.start(self.organization)
        ledger.record(self.organization, self.product, ledger.Kind.OPENING, ledger.OWN, 3)
        self.meli = FakeSellFlow()
        patcher = patch("meli.client.urlopen", side_effect=self.meli)
        patcher.start()
        self.addCleanup(patcher.stop)

    def add(self, **fields):
        data = {"action": "add", "add-url": URL, "add-product": str(self.product.pk), "add-price": "65000", "add-quantity": "1"}
        data.update({f"add-{key}": value for key, value in fields.items()})
        return self.client.post("/catalog/meli/new/", data)

    def draft(self):
        return ListingDraft.objects.get(organization=self.organization)


class BodyTests(DraftTestCase):
    def test_the_body_matches_the_sell_flow_defaults(self):
        draft = ListingDraft(
            organization=self.organization, product=self.product, catalog_product_id=CATALOG,
            catalog_name="Caramel Dream Sweet Tooth 75 Ml",
            category_id="MLA1271", has_regulatory=True, price=Decimal("65000.00"), quantity=2,
        )
        body = build_item(draft, defaults_for(self.organization))
        self.assertEqual(body["catalog_product_id"], CATALOG)
        self.assertEqual(body["title"], "Caramel Dream Sweet Tooth 75 Ml")
        self.assertIs(body["catalog_listing"], True)
        self.assertEqual((body["price"], body["currency_id"], body["available_quantity"]), (65000, "ARS", 2))
        self.assertEqual(body["listing_type_id"], "gold_special")
        self.assertEqual(body["shipping"], {"mode": "me2", "local_pick_up": False, "free_shipping": True})
        draft.price = Decimal("15999")
        self.assertIs(build_item(draft, defaults_for(self.organization))["shipping"]["free_shipping"], False)
        draft.price = Decimal("33000")
        self.assertIs(build_item(draft, defaults_for(self.organization))["shipping"]["free_shipping"], True)
        self.assertIn({"id": "INVOICE", "value_id": "6891888"}, body["sale_terms"])
        self.assertIn({"id": "WARRANTY_TYPE", "value_id": "2230280"}, body["sale_terms"])
        self.assertIn({"id": "WARRANTY_TIME", "value_name": "30 días"}, body["sale_terms"])
        self.assertIn({"id": "SELLER_SKU", "value_name": self.product.sku}, body["attributes"])
        self.assertIn({"id": "ITEM_CONDITION", "value_id": "2230284"}, body["attributes"])
        self.assertIn({"id": "155_98_RESOLUTION_FILE_NUMBER", "value_id": "-1"}, body["attributes"])

    def test_a_draft_with_its_own_settings_uses_them_and_pickup_stays_off(self):
        draft = ListingDraft(
            organization=self.organization, product=self.product, catalog_product_id=CATALOG,
            price=Decimal("1.50"), quantity=1, use_defaults=False, installments=True, invoice_a=True,
            warranty=ListingDraft.Warranty.FACTORY, warranty_days=90,
        )
        body = build_item(draft, settings_of(draft, defaults_for(self.organization)))
        self.assertEqual(body["listing_type_id"], "gold_pro")
        self.assertEqual(body["price"], 1.5)
        self.assertIn({"id": "INVOICE", "value_id": "6891885"}, body["sale_terms"])
        self.assertIn({"id": "WARRANTY_TYPE", "value_id": "2230279"}, body["sale_terms"])
        self.assertIn({"id": "WARRANTY_TIME", "value_name": "90 días"}, body["sale_terms"])
        self.assertIs(body["shipping"]["local_pick_up"], False)
        # The category doesn't ask for the 155/98 number: it isn't sent.
        self.assertNotIn("155_98_RESOLUTION_FILE_NUMBER", [attribute["id"] for attribute in body["attributes"]])


class DraftFlowTests(DraftTestCase):
    def test_a_catalog_url_becomes_a_checked_ready_draft(self):
        response = self.add()
        self.assertRedirects(response, "/catalog/meli/new/")
        draft = self.draft()
        self.assertEqual(draft.status, ListingDraft.Status.READY, draft.error)
        self.assertEqual((draft.catalog_product_id, draft.category_id), (CATALOG, "MLA1271"))
        self.assertEqual(draft.catalog_name, "Caramel Dream Sweet Tooth 75 Ml")
        self.assertTrue(draft.has_regulatory)
        self.assertEqual(self.meli.validated[-1]["category_id"], "MLA1271")
        self.assertEqual(self.meli.validated[-1]["title"], "Caramel Dream Sweet Tooth 75 Ml")
        self.assertEqual(self.meli.created, [])
        page = self.client.get("/catalog/meli/new/")
        self.assertContains(page, "Caramel Dream Sweet Tooth 75 Ml")
        self.assertContains(page, "Post all ready (1)")

    def test_mercado_libre_refusing_the_check_shows_its_reason(self):
        self.meli.refuse = "The price is lower than the minimum allowed."
        self.add()
        draft = self.draft()
        self.assertEqual(draft.status, ListingDraft.Status.NEEDS_FIX)
        self.assertEqual(draft.error, "The price is lower than the minimum allowed.")

    def test_a_refusal_keeps_mercado_libres_whole_answer_and_what_was_sent(self):
        self.meli.refuse = "User has not mode me1"
        self.add()
        draft = self.draft()
        details = draft.error_details
        self.assertEqual((details["call"], details["status"]), ("POST /items/validate", 400))
        self.assertEqual(details["response"]["error"], "validation_error")
        self.assertEqual([cause["code"] for cause in details["response"]["cause"]], ["item.title.length", "item.shipping.mode.invalid"])
        self.assertEqual(details["sent"]["shipping"], {"mode": "me2", "local_pick_up": False, "free_shipping": True})
        # The error is the reason, not the warning listed after it.
        self.assertEqual(draft.error, "User has not mode me1")
        self.assertEqual(details["sent"]["catalog_product_id"], CATALOG)
        for url in ("/catalog/meli/new/", f"/catalog/meli/new/{draft.pk}/"):
            page = self.client.get(url)
            self.assertContains(page, "<summary>Details</summary>", html=False)
            self.assertContains(page, "item.shipping.mode.invalid")
            self.assertContains(page, f'data-copy-target="error-details-{draft.pk}"')
        # Checked again and accepted: the details go away.
        self.meli.refuse = ""
        self.client.post(
            f"/catalog/meli/new/{draft.pk}/",
            {"price": "65000", "quantity": "1", "use_defaults": "on", "condition": "new", "warranty": "seller", "warranty_days": "30"},
        )
        draft.refresh_from_db()
        self.assertEqual((draft.status, draft.error_details), (ListingDraft.Status.READY, {}))
        self.assertNotContains(self.client.get("/catalog/meli/new/"), "<summary>Details</summary>", html=False)

    def test_warnings_alone_dont_block_the_draft(self):
        self.meli.warn_only = True
        self.add()
        draft = self.draft()
        self.assertEqual(draft.status, ListingDraft.Status.READY)
        self.assertEqual(draft.error, "Mercado Libre warns: User has not mode me1; Free shipping costs exceeds sale")
        self.assertEqual(draft.error_details["response"]["cause"][0]["code"], "shipping.lost_me1_by_user")
        page = self.client.get("/catalog/meli/new/")
        self.assertContains(page, '<p class="muted">Mercado Libre warns:')
        self.assertContains(page, "Post all ready (1)")
        self.client.post("/catalog/meli/new/", {"action": "post_all"})
        draft.refresh_from_db()
        self.assertEqual((draft.status, draft.error_details), (ListingDraft.Status.PUBLISHED, {}))

    def test_the_draft_number_shows_and_heads_the_copied_details(self):
        self.meli.refuse = "Nope"
        self.add()
        draft = self.draft()
        self.assertTrue(draft.error_details_text().startswith('{\n  "draft": %d,' % draft.pk))
        self.assertContains(self.client.get("/catalog/meli/new/"), f"Draft #{draft.pk}")
        self.assertContains(self.client.get(f"/catalog/meli/new/{draft.pk}/"), f"Draft #{draft.pk}")

    def test_an_answer_that_isnt_json_is_kept_as_text(self):
        self.meli.refuse = "x"
        self.meli.refuse_text = "<html>Bad gateway</html>"
        self.add()
        draft = self.draft()
        self.assertEqual(draft.error_details["status"], 502)
        self.assertEqual(draft.error_details["response"], "<html>Bad gateway</html>")

    def test_a_refused_post_keeps_its_details_too(self):
        self.add()
        self.meli.refuse = "Item is already listed in this catalog product."
        self.client.post("/catalog/meli/new/", {"action": "post_all"})
        details = self.draft().error_details
        self.assertEqual((details["call"], details["sent"]["available_quantity"]), ("POST /items", 1))

    def test_an_inactive_catalog_product_needs_a_fix(self):
        self.meli.product_status = "inactive"
        self.add()
        self.assertEqual(self.draft().status, ListingDraft.Status.NEEDS_FIX)
        self.assertEqual(self.meli.validated, [])

    def test_quantity_defaults_to_one_and_never_passes_stock(self):
        page = self.client.get("/catalog/meli/new/")
        self.assertContains(page, 'name="add-quantity" value="1"')
        self.assertContains(page, "(3 in stock)")
        response = self.add(quantity="4")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Only 3 in stock in the Depósito.")
        self.assertFalse(ListingDraft.objects.exists())

    def test_a_product_without_stock_cant_get_a_draft(self):
        ledger.record(self.organization, self.product, ledger.Kind.DAMAGE, ledger.OWN, -3)
        response = self.add()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ListingDraft.objects.exists())
        self.assertContains(response, "No product has stock in the Depósito yet.")

    def test_without_a_first_count_nothing_can_be_drafted(self):
        organization = shared_seller("Fresh")
        self.client.force_login(member(organization, username="bea"))
        page = self.client.get("/catalog/meli/new/")
        self.assertContains(page, "count it first")
        self.assertNotContains(page, "From a Mercado Libre catalog link")

    def test_posting_creates_the_item_turns_flex_off_and_links_the_listing(self):
        self.add()
        draft = self.draft()
        response = self.client.post("/catalog/meli/new/", {"action": "post_all"})
        self.assertRedirects(response, "/catalog/meli/new/")
        draft.refresh_from_db()
        self.assertEqual(draft.status, ListingDraft.Status.PUBLISHED, draft.error)
        self.assertEqual(draft.item_id, "MLA900")
        self.assertEqual(len(self.meli.created), 1)
        self.assertEqual(self.meli.flex_removed, ["MLA900"])
        listing = MeliListing.objects.get(organization=self.organization, item_id="MLA900")
        self.assertEqual(listing.product, self.product)
        # Posting again creates nothing: the draft is published.
        self.client.post("/catalog/meli/new/", {"action": "post_all"})
        self.client.post("/catalog/meli/new/", {"action": "post", "draft": [str(draft.pk)]})
        self.assertEqual(len(self.meli.created), 1)

    def test_post_selected_posts_only_the_ticked_ready_drafts(self):
        self.add()
        self.add()
        first, second = ListingDraft.objects.order_by("pk")
        self.client.post("/catalog/meli/new/", {"action": "post", "draft": [str(second.pk)]})
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((first.status, second.status), (ListingDraft.Status.READY, ListingDraft.Status.PUBLISHED))
        self.assertEqual(len(self.meli.created), 1)

    def test_stock_gone_before_posting_fails_without_calling_mercado_libre(self):
        self.add()
        ledger.record(self.organization, self.product, ledger.Kind.DAMAGE, ledger.OWN, -3)
        self.client.post("/catalog/meli/new/", {"action": "post_all"})
        draft = self.draft()
        self.assertEqual(draft.status, ListingDraft.Status.FAILED)
        self.assertEqual(draft.error, "No stock left in the Depósito.")
        self.assertEqual(self.meli.created, [])

    def test_a_refused_post_keeps_mercado_libres_reason(self):
        self.add()
        self.meli.refuse = "Item is already listed in this catalog product."
        self.client.post("/catalog/meli/new/", {"action": "post_all"})
        draft = self.draft()
        self.assertEqual(draft.status, ListingDraft.Status.FAILED)
        self.assertEqual(draft.error, "Item is already listed in this catalog product.")

    def test_editing_a_draft_with_its_own_settings_checks_it_again(self):
        self.add()
        draft = self.draft()
        page = self.client.get(f"/catalog/meli/new/{draft.pk}/")
        self.assertContains(page, 'id="own-settings" hidden')
        response = self.client.post(
            f"/catalog/meli/new/{draft.pk}/",
            {
                "price": "70000", "quantity": "2", "condition": "new", "warranty": "seller", "warranty_days": "60",
                "installments": "on", "regulatory_na": "on",
            },
        )
        self.assertRedirects(response, "/catalog/meli/new/")
        draft.refresh_from_db()
        self.assertEqual(draft.status, ListingDraft.Status.READY)
        self.assertFalse(draft.use_defaults)
        self.assertEqual(self.meli.validated[-1]["listing_type_id"], "gold_pro")
        self.assertIn({"id": "WARRANTY_TIME", "value_name": "60 días"}, self.meli.validated[-1]["sale_terms"])
        too_many = self.client.post(
            f"/catalog/meli/new/{draft.pk}/",
            {"price": "70000", "quantity": "9", "use_defaults": "on", "condition": "new", "warranty": "seller", "warranty_days": "30"},
        )
        self.assertContains(too_many, "Only 3 in stock in the Depósito.")

    def test_a_published_draft_cant_be_edited_or_deleted(self):
        self.add()
        self.client.post("/catalog/meli/new/", {"action": "post_all"})
        draft = self.draft()
        self.client.post(f"/catalog/meli/new/{draft.pk}/", {"action": "delete"})
        self.assertTrue(ListingDraft.objects.filter(pk=draft.pk).exists())


class DraftAccessTests(DraftTestCase):
    def test_another_organizations_drafts_and_products_stay_out(self):
        other = shared_seller("Other", user_id="777")
        theirs = product(other, variant="75ML")
        ledger.start(other)
        ledger.record(other, theirs, ledger.Kind.OPENING, ledger.OWN, 5)
        their_draft = ListingDraft.objects.create(
            organization=other, product=theirs, catalog_product_id=CATALOG, price=1, status=ListingDraft.Status.READY
        )
        page = self.client.get("/catalog/meli/new/")
        self.assertNotContains(page, f'value="{theirs.pk}"')
        self.assertEqual(self.client.get(f"/catalog/meli/new/{their_draft.pk}/").status_code, 404)
        self.assertEqual(self.add(product=str(theirs.pk)).status_code, 200)
        self.assertFalse(ListingDraft.objects.filter(organization=self.organization).exists())
        self.client.post("/catalog/meli/new/", {"action": "post", "draft": [str(their_draft.pk)]})
        self.client.post("/catalog/meli/new/", {"action": "post_all"})
        their_draft.refresh_from_db()
        self.assertEqual(their_draft.status, ListingDraft.Status.READY)
        self.assertEqual(self.meli.created, [])

    def test_contributors_read_but_cannot_change_anything(self):
        self.add()
        draft = self.draft()
        self.client.force_login(member(self.organization, role=Membership.Role.CONTRIBUTOR, username="bea"))
        page = self.client.get("/catalog/meli/new/")
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "Post all ready")
        for data in ({"action": "post_all"}, {"action": "defaults"}, {"action": "add"}):
            self.assertEqual(self.client.post("/catalog/meli/new/", data).status_code, 403)
        self.assertEqual(self.client.post(f"/catalog/meli/new/{draft.pk}/", {"action": "delete"}).status_code, 403)
        self.assertEqual(self.meli.created, [])

    def test_without_catalog_access_the_pages_are_closed(self):
        self.client.force_login(member(self.organization, apps=(), username="cid"))
        self.assertEqual(self.client.get("/catalog/meli/new/").status_code, 403)

    def test_saving_the_defaults_checks_the_drafts_that_follow_them_again(self):
        self.add()
        before = len(self.meli.validated)
        response = self.client.post(
            "/catalog/meli/new/",
            {"action": "defaults", "defaults-condition": "new", "defaults-warranty": "seller", "defaults-warranty_days": "15", "defaults-invoice_a": "on"},
        )
        self.assertRedirects(response, "/catalog/meli/new/")
        self.assertEqual(len(self.meli.validated), before + 1)
        self.assertIn({"id": "INVOICE", "value_id": "6891885"}, self.meli.validated[-1]["sale_terms"])
        self.assertIn({"id": "WARRANTY_TIME", "value_name": "15 días"}, self.meli.validated[-1]["sale_terms"])
