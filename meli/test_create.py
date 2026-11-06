from unittest.mock import patch

from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from accounts.models import Membership
from catalog.test_views import member
from catalog.tests import product
from inventory import ledger
from inventory.tests import listing

from .models import ListingDraft, ListingTemplate
from .test_drafts import CATALOG, URL
from .test_product_drafts import PerfumeDraftCase
from .tests import shared_seller


class SectionTests(PerfumeDraftCase):
    """Mercado Libre has one place in the menu: Connect, Listings, Create listing."""

    def test_the_menu_and_tabs_lead_to_listings_and_create_listing(self):
        page = self.client.get("/catalog/meli/new/")
        self.assertContains(page, 'class="nav-group" open')
        self.assertContains(page, 'href="/catalog/meli/">Listings</a>', html=False)
        self.assertContains(page, "Create a Mercado Libre listing")
        catalog = self.client.get("/catalog/")
        self.assertNotContains(catalog, 'class="tab" href="/catalog/meli/"')
        self.assertNotContains(catalog, 'class="nav-group" open')

    def test_without_catalog_access_only_connect_shows(self):
        self.client.force_login(member(self.organization, role=Membership.Role.CONTRIBUTOR, apps=(), username="cid"))
        page = self.client.get("/organization/")
        self.assertContains(page, '<span class="nav-text">Connect</span>', html=False)
        self.assertNotContains(page, 'href="/catalog/meli/new/"')
        self.assertNotContains(page, 'href="/catalog/meli/"')


class StartTabsTests(PerfumeDraftCase):
    """One way to start shows at a time; a refused catalog link stays on its tab."""

    def test_only_the_chosen_way_shows(self):
        page = self.client.get("/catalog/meli/new/")
        self.assertContains(page, 'id="pick-products"')
        self.assertNotContains(page, 'name="add-url"')
        self.assertNotContains(page, "No templates yet")
        page = self.client.get("/catalog/meli/new/", {"start": "link"})
        self.assertContains(page, 'name="add-url"')
        self.assertNotContains(page, 'id="pick-products"')
        self.assertContains(self.client.get("/catalog/meli/new/", {"start": "template"}), "No templates yet")
        self.assertContains(self.add(product="", url="https://example.com/x"), 'name="add-url"')


class PickTests(PerfumeDraftCase):
    def pick(self, **params):
        return self.client.get("/catalog/meli/new/pick/", params)

    def test_products_come_with_their_button_or_why_not(self):
        unpriced = product(self.organization, variant="30ML")
        other = shared_seller("Other")
        product(other, variant="50ML")
        page = self.pick()
        self.assertContains(page, self.product.sku)
        self.assertContains(page, f'action="/catalog/meli/products/{self.product.pk}/draft/"', html=False)
        self.assertContains(page, unpriced.sku)
        self.assertContains(page, "Set a price first.")
        self.assertNotContains(page, "50ML")
        self.assertNotContains(self.pick(q="30ML"), self.product.sku)

    def test_a_contributor_sees_no_buttons(self):
        self.client.force_login(member(self.organization, role=Membership.Role.CONTRIBUTOR, username="cid"))
        page = self.pick()
        self.assertContains(page, self.product.sku)
        self.assertNotContains(page, "Create listing")

    def test_more_rows_take_no_more_queries(self):
        with CaptureQueriesContext(connection) as one:
            self.pick()
        for variant in ("10ML", "20ML", "40ML"):
            row = product(self.organization, variant=variant, list_price=1000)
            ledger.record(self.organization, row, ledger.Kind.OPENING, ledger.OWN, 2)
        with CaptureQueriesContext(connection) as four:
            self.pick()
        self.assertEqual(len(four), len(one))


class TemplateStartTests(PerfumeDraftCase):
    def setUp(self):
        super().setUp()
        self.source = listing(self.organization, "MLA900", title="Perfume template", category_id="MLA1271", status="paused")
        self.template = ListingTemplate.objects.create(
            organization=self.organization, category_id="MLA1271", item_id="MLA900", title="Perfume template",
            state=ListingTemplate.State.READY,
        )

    @override_settings(MISTRAL_API_KEY="test-key")
    def test_a_draft_from_a_template_takes_its_category_and_copies_once_loaded(self):
        self.product.meli_category_id = ""
        self.product.save()
        self.assertContains(self.client.get("/catalog/meli/new/", {"start": "template"}), "Perfume template")
        response = self.client.post(f"/catalog/meli/products/{self.product.pk}/draft/", {"template": self.template.pk})
        draft = ListingDraft.objects.get()
        self.assertRedirects(response, f"/catalog/meli/new/{draft.pk}/?copy={self.source.pk}", fetch_redirect_response=False)
        self.assertEqual((draft.kind, draft.category_id), (ListingDraft.Kind.PRODUCT, "MLA1271"))
        page = self.client.get(f"/catalog/meli/new/{draft.pk}/?copy={self.source.pk}")
        self.assertContains(page, 'id="template-copy"')
        self.assertContains(page, f'"listing": "{self.source.pk}"', html=False)
        self.assertNotContains(self.client.get(f"/catalog/meli/new/{draft.pk}/"), 'id="template-copy"')

    def test_the_product_page_offers_its_categorys_template(self):
        actions = self.client.get(f"/catalog/meli/products/{self.product.pk}/")
        self.assertContains(actions, "Create from the MLA1271 template")
        self.assertContains(self.client.get("/catalog/meli/new/pick/", {"template": self.template.pk}), "Create from template")

    def test_another_organizations_template_is_not_found(self):
        other = shared_seller("Other")
        foreign = ListingTemplate.objects.create(
            organization=other, category_id="MLA1271", item_id="MLA901", state=ListingTemplate.State.READY
        )
        response = self.client.post(f"/catalog/meli/products/{self.product.pk}/draft/", {"template": foreign.pk})
        self.assertEqual(response.status_code, 404)
        self.assertFalse(ListingDraft.objects.exists())
        self.assertEqual(self.client.get("/catalog/meli/new/pick/", {"template": foreign.pk}).status_code, 404)

    def test_listings_filter_shows_templates(self):
        listing(self.organization, "MLA777", title="Ordinary listing", category_id="MLA1271", status="active")
        page = self.client.get("/catalog/meli/", {"link": "template"})
        self.assertContains(page, "Perfume template")
        self.assertNotContains(page, "Ordinary listing")


class CatalogLinkTests(PerfumeDraftCase):
    def test_a_link_without_a_product_uses_the_one_imported_from_it(self):
        self.product.meli_catalog_product_id = CATALOG
        self.product.save()
        self.add(product="")
        self.assertEqual(self.draft().product, self.product)

    def test_an_unknown_link_offers_the_import_with_the_link(self):
        response = self.add(product="")
        self.assertFalse(ListingDraft.objects.exists())
        self.assertContains(response, "No Vimple product came from this link.")
        self.assertContains(response, "/catalog/import/?url=https%3A//www.mercadolibre.com.ar/", html=False)

    def test_the_import_page_reads_a_link_it_is_given(self):
        values = {
            "title": "Caramel Dream 75 ml", "brand_hint": "", "variant": "75ML", "gtin": "", "description": "",
            "meli_catalog_product_id": CATALOG, "meli_category_id": "MLA1271", "pictures": [], "attributes": {},
        }
        with patch("meli.import_views.read", return_value=values) as read:
            page = self.client.get("/catalog/import/", {"url": URL})
        read.assert_called_once()
        self.assertEqual(read.call_args.args[1], URL)
        self.assertContains(page, "Caramel Dream 75 ml")


class ListedSummaryTests(PerfumeDraftCase):
    """How a product is already listed shows where a new listing starts, and opens the listings."""

    def setUp(self):
        super().setUp()
        listing(self.organization, "MLA1", self.product, variation_id="11", title="Perfume A", status="active")
        listing(self.organization, "MLA1", self.product, variation_id="12", title="Perfume A", status="active")
        listing(self.organization, "MLA2", self.product, title="Perfume B", status="paused")
        ListingTemplate.objects.create(
            organization=self.organization, category_id="MLA1271", item_id="MLA2", state=ListingTemplate.State.READY
        )

    def test_the_picker_counts_items_per_status_and_links_them(self):
        unlisted = product(self.organization, variant="30ML")
        page = self.client.get("/catalog/meli/new/pick/")
        self.assertContains(page, "(1 active, 1 paused)")
        self.assertContains(page, f"/catalog/meli/listed/?product={self.product.pk}")
        self.assertContains(page, "Not listed yet")
        self.assertNotContains(page, f"/catalog/meli/listed/?product={unlisted.pk}")

    def test_the_product_page_shows_the_same_summary(self):
        actions = self.client.get(f"/catalog/meli/products/{self.product.pk}/")
        self.assertContains(actions, "(1 active, 1 paused)")
        self.assertContains(actions, 'id="listed-dialog"')

    def test_the_dialog_lists_this_products_listings_only(self):
        other = shared_seller("Other")
        listing(other, "MLA9", product(other, variant="75ML"), title="Someone else's", status="active")
        page = self.client.get("/catalog/meli/listed/", {"product": self.product.pk})
        self.assertContains(page, "MLA1", count=2)
        self.assertContains(page, "Perfume B")
        self.assertContains(page, '<span class="pill">Template</span>', html=True)
        self.assertNotContains(page, "Someone else")
        foreign = product(other, variant="10ML")
        self.assertEqual(self.client.get("/catalog/meli/listed/", {"product": foreign.pk}).status_code, 404)
