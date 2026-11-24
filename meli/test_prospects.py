import json
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings

from accounts.models import Membership, Organization, OrganizationApiKey
from catalog.test_suggest import FakeMistral
from catalog.test_views import member
from catalog.tests import product

from .models import ListingDraft, MeliListing, MeliListingProspect
from .test_copy import GENDER, NAME
from .test_product_drafts import PERFUME_ATTRIBUTES, PerfumeDraftCase
from .tests import shared_seller

URL = "/api/meli/prospects"
PAGE_URL = "/catalog/meli/new/?start=scraped"

# As the scraper sends it (ml-products.json): a list of pages.
SCRAPED = [
    {
        "id": "MLAU3250631132",
        "productId": "MLAU3250631132",
        "title": "Kit Básico De Insumos Descartables Para Pestañas Profesional",
        "currentPrice": 15000,
        "pageUrl": "https://www.mercadolibre.com.ar/kit-basico-de-insumos/up/MLAU3250631132",
        "images": {"gallery": ["https://http2.mlstatic.com/a.webp", "https://http2.mlstatic.com/b.jpg"]},
        "seller": "BEAUTYBODYS",
        "scrapedAt": "2026-11-24T04:46:06.476Z",
        "characteristicGroups": {"Características principales": {"Color": "Rosa", "Marca": "City Girl"}},
        "characteristics": {"Color": "Rosa", "Marca": "City Girl", "Material": "Sintético"},
    }
]


class ProspectApiTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Seller")
        self.owner = member(self.organization)
        self.key = OrganizationApiKey.generate(self.organization, self.owner)

    def post(self, body, organization=None, key=None):
        return self.client.post(
            f"{URL}?organization={organization or self.organization.pk}",
            data=body if isinstance(body, str) else json.dumps(body),
            content_type="application/json",
            headers={"Authorization": f"Bearer {key or self.key}"},
        )

    def test_the_scraped_file_is_stored_as_pending_and_a_repeat_updates_it(self):
        response = self.post(SCRAPED)
        self.assertEqual(response.status_code, 201)
        prospect = MeliListingProspect.objects.get()
        self.assertEqual(response.json(), {
            "created": 1, "updated": 0,
            "prospects": [{"id": prospect.pk, "external_id": "MLAU3250631132", "status": "pending", "created": True}],
        })
        self.assertEqual(prospect.organization, self.organization)
        self.assertEqual(prospect.status, MeliListingProspect.Status.PENDING)
        self.assertEqual(prospect.payload, SCRAPED[0])
        self.assertEqual((prospect.price, prospect.seller), (Decimal("15000"), "BEAUTYBODYS"))
        self.assertEqual(prospect.picture, "https://http2.mlstatic.com/a.webp")
        self.assertEqual(prospect.scraped_at.year, 2026)
        self.assertEqual(prospect.characteristics()["Material"], "Sintético")
        self.assertIsNotNone(OrganizationApiKey.objects.get().last_used_at)

        prospect.status = MeliListingProspect.Status.DISMISSED
        prospect.save()
        again = self.post({**SCRAPED[0], "currentPrice": 14000})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["updated"], 1)
        prospect.refresh_from_db()
        self.assertEqual((prospect.price, prospect.status), (Decimal("14000"), MeliListingProspect.Status.DISMISSED))
        self.assertEqual(MeliListingProspect.objects.count(), 1)

    def test_the_same_product_id_replaces_the_json(self):
        self.assertEqual(self.post(SCRAPED).status_code, 201)
        newer = {"id": "another-id", "productId": "MLAU3250631132", "title": "Kit nuevo", "currentPrice": 9000}
        response = self.post([newer])
        self.assertEqual((response.status_code, response.json()["updated"]), (200, 1))
        prospect = MeliListingProspect.objects.get()
        self.assertEqual(prospect.payload, newer)
        self.assertEqual((prospect.title, prospect.price), ("Kit nuevo", Decimal("9000")))
        # Any productId keys it, as sent; snake_case too.
        self.assertEqual(self.post({"product_id": "sku-123", "title": "Otro"}).status_code, 201)
        self.assertEqual(MeliListingProspect.objects.filter(external_id="sku-123").count(), 1)
        self.assertEqual(self.post({"productId": "x" * 33}).status_code, 422)

    def test_the_id_can_come_from_the_page_url(self):
        entry = {"title": "Item page", "pageUrl": "https://articulo.mercadolibre.com.ar/MLA-1234567890-perfume-_JM"}
        self.assertEqual(self.post(entry).status_code, 201)
        self.assertEqual(MeliListingProspect.objects.get().external_id, "MLA1234567890")

    def test_a_bad_entry_stores_nothing(self):
        response = self.post([SCRAPED[0], {"title": "No id"}])
        self.assertEqual(response.status_code, 422)
        self.assertIn("Listing 1", response.json()["detail"])
        self.assertEqual(self.post("not json").status_code, 400)
        self.assertEqual(self.post([]).status_code, 422)
        self.assertFalse(MeliListingProspect.objects.exists())

    def test_a_key_reaches_only_its_own_organization(self):
        other = Organization.objects.create(name="Other")
        other_key = OrganizationApiKey.generate(other, member(other, username="olga"))
        self.assertEqual(self.post(SCRAPED, organization=other.pk).status_code, 404)
        self.assertEqual(self.post(SCRAPED, key="vk_wrong").status_code, 401)
        self.assertFalse(MeliListingProspect.objects.exists())
        # The same page in two organizations is two rows; neither moves the other.
        self.assertEqual(self.post(SCRAPED).status_code, 201)
        self.assertEqual(self.post(SCRAPED, organization=other.pk, key=other_key).status_code, 201)
        self.assertEqual(MeliListingProspect.objects.filter(external_id="MLAU3250631132").count(), 2)
        self.assertEqual(MeliListingProspect.objects.get(organization=self.organization).organization, self.organization)


def prospect_for(organization, **fields):
    return MeliListingProspect.objects.create(
        organization=organization,
        external_id=fields.pop("external_id", "MLAU3250631132"),
        title=fields.pop("title", SCRAPED[0]["title"]),
        payload=fields.pop("payload", SCRAPED[0]),
        **fields,
    )


@override_settings(MISTRAL_API_KEY="test-key")
class ProspectPageTests(PerfumeDraftCase):
    def setUp(self):
        super().setUp()
        self.meli.category_attributes = [*PERFUME_ATTRIBUTES, NAME, GENDER]
        self.prospect = prospect_for(self.organization, payload={
            "characteristics": {"Marca": "Lattafa", "Género": "Mujer", "Nombre del perfume": "Yara", "Material": "Vidrio"},
        })

    def test_the_tab_lists_this_organizations_pending_prospects(self):
        other = shared_seller("Other", user_id="777")
        prospect_for(other, external_id="MLAU999", title="Theirs")
        prospect_for(self.organization, external_id="MLAU2", title="Old one", status=MeliListingProspect.Status.DISMISSED)
        page = self.client.get(PAGE_URL)
        self.assertContains(page, "From a scraped listing")
        self.assertContains(page, self.prospect.title)
        self.assertNotContains(page, "Theirs")
        self.assertNotContains(page, "Old one")
        self.assertContains(self.client.get("/catalog/meli/new/?start=scraped&status=dismissed"), "Old one")
        self.assertNotContains(self.client.get("/catalog/meli/new/?start=scraped&status=all"), "Theirs")
        self.assertNotContains(self.client.get("/catalog/meli/"), "Prospects")

    def test_linking_by_sku_stays_in_the_organization(self):
        other = shared_seller("Other", user_id="777")
        theirs = product(other, variant="50ML")
        self.client.post(PAGE_URL, {"action": "scraped_link", "prospect": self.prospect.pk, "sku": theirs.sku})
        self.prospect.refresh_from_db()
        self.assertIsNone(self.prospect.product)
        self.client.post(PAGE_URL, {"action": "scraped_link", "prospect": self.prospect.pk, "sku": self.product.sku.lower()})
        self.prospect.refresh_from_db()
        self.assertEqual(self.prospect.product, self.product)
        their_prospect = prospect_for(other, external_id="MLAU999")
        response = self.client.post(PAGE_URL, {"action": "scraped_dismiss", "prospect": their_prospect.pk})
        self.assertEqual(response.status_code, 404)
        self.client.post(PAGE_URL, {"action": "dismiss_selected", "selected": [their_prospect.pk]})
        their_prospect.refresh_from_db()
        self.assertEqual(their_prospect.status, MeliListingProspect.Status.PENDING)

    def test_contributors_neither_see_nor_change_them(self):
        self.client.force_login(member(self.organization, role=Membership.Role.CONTRIBUTOR, username="bea"))
        self.assertNotContains(self.client.get(PAGE_URL), self.prospect.title)
        self.assertEqual(self.client.post(PAGE_URL, {"action": "scraped_dismiss", "prospect": self.prospect.pk}).status_code, 403)
        self.assertEqual(self.client.post(f"/catalog/meli/new/scraped/{self.prospect.pk}/").status_code, 403)

    def test_create_listing_starts_a_draft_that_copies_the_characteristics_by_name(self):
        # Not linked yet: no draft.
        self.assertEqual(self.client.post(f"/catalog/meli/new/scraped/{self.prospect.pk}/").status_code, 404)
        self.prospect.product = self.product
        self.prospect.save()
        self.assertContains(self.client.get(PAGE_URL), "Create listing")
        response = self.client.post(f"/catalog/meli/new/scraped/{self.prospect.pk}/")
        draft = self.draft()
        self.assertRedirects(response, f"/catalog/meli/new/{draft.pk}/?prospect={self.prospect.pk}", fetch_redirect_response=False)
        self.assertEqual((draft.kind, draft.product), (ListingDraft.Kind.PRODUCT, self.product))
        self.prospect.refresh_from_db()
        self.assertEqual((self.prospect.status, self.prospect.draft), (MeliListingProspect.Status.DRAFTED, draft))
        page = self.client.get(f"/catalog/meli/new/{draft.pk}/?prospect={self.prospect.pk}")
        self.assertContains(page, f'hx-vals=\'{{"prospect": "{self.prospect.pk}"}}\'', html=False)

        # The scraped listing's category, looked up when the tab opened (a guess from its title here).
        self.assertEqual(draft.category_id, self.prospect.category_id)
        self.assertEqual(self.prospect.category_path, "Belleza y Cuidado Personal > Perfumes")

        fake = FakeMistral({"values": {"PERFUME_NAME": "Yara", "GENDER": "mujer", "PERFUME_TYPE": "Colonia"}})
        with patch("vimple.ai.urlopen", side_effect=fake):
            copied = self.client.post(f"/catalog/meli/new/{draft.pk}/copy/", {"prospect": self.prospect.pk})
        sent = json.loads(fake.asked[0]["messages"][1]["content"])
        # Every characteristic goes; the AI fills the empty fields by meaning, never brand or what the product has.
        self.assertEqual(sent["scraped_listing"]["characteristics"]["Material"], "Vidrio")
        asked = {item["id"] for item in sent["attributes"]}
        self.assertTrue({"GENDER", "PERFUME_NAME", "PERFUME_TYPE"} <= asked)
        self.assertFalse(asked & {"BRAND", "MODEL", "GTIN", "UNIT_VOLUME", "OLFACTORY_NOTES", "SELLER_SKU"})
        self.assertContains(copied, "Copied 2 values")
        self.assertContains(copied, '<option value="Mujer" selected>Mujer</option>', html=False)
        self.assertContains(copied, "<strong>Tipo de perfume</strong>: not one of Mercado Libre&#x27;s values", html=False)

        other = shared_seller("Other", user_id="777")
        theirs = prospect_for(other, external_id="MLAU999")
        with patch("vimple.ai.urlopen", side_effect=fake):
            self.assertEqual(self.client.post(f"/catalog/meli/new/{draft.pk}/copy/", {"prospect": theirs.pk}).status_code, 404)

    def test_a_product_that_cant_be_listed_says_why(self):
        self.product.list_price = None
        self.product.save()
        self.prospect.product = self.product
        self.prospect.save()
        self.assertContains(self.client.get(PAGE_URL), "Set a price first.")
        self.client.post(f"/catalog/meli/new/scraped/{self.prospect.pk}/")
        self.assertFalse(ListingDraft.objects.exists())

    def test_the_copy_dialog_offers_the_prospect_again_and_nothing_to_copy_says_only_that(self):
        self.prospect.product = self.product
        self.prospect.save()
        self.client.post(f"/catalog/meli/new/scraped/{self.prospect.pk}/")
        draft = self.draft()
        page = self.client.get(f"/catalog/meli/new/{draft.pk}/")
        self.assertContains(page, f'hx-vals=\'{{"prospect": "{self.prospect.pk}"}}\'', html=False)
        self.assertContains(page, '<span class="pill">Scraped listing</span>', html=False)
        # Another draft of the same product doesn't offer this prospect.
        self.prospect.draft = None
        self.prospect.save()
        self.assertNotContains(self.client.get(f"/catalog/meli/new/{draft.pk}/"), '<span class="pill">Scraped listing</span>', html=False)
        empty = prospect_for(self.organization, external_id="MLAU5", payload={"characteristics": {"Marca": "Lattafa"}})
        with patch("vimple.ai.urlopen", side_effect=FakeMistral({"values": {}})):
            copied = self.client.post(f"/catalog/meli/new/{draft.pk}/copy/", {"prospect": empty.pk})
        self.assertContains(copied, "Nothing to copy from")
        self.assertNotContains(copied, "Copied 0 values")

    def test_copying_from_the_scraped_listing_switches_to_its_category_then_copies(self):
        self.prospect.product = self.product
        self.prospect.save()
        self.client.post(f"/catalog/meli/new/scraped/{self.prospect.pk}/")
        draft = self.draft()
        MeliListingProspect.objects.filter(pk=self.prospect.pk).update(category_id="MLA5555", category_path="Belleza > Pestañas")
        page = self.client.get(f"/catalog/meli/new/{draft.pk}/")
        self.assertContains(page, "Switches this draft to its category, MLA5555 (Belleza &gt; Pestañas)")
        response = self.client.post(
            f"/catalog/meli/new/{draft.pk}/scraped/", {"prospect": self.prospect.pk, "attr_key": ["PERFUME_NAME"], "attr_value": ["Mío"]}
        )
        self.assertEqual(response["HX-Redirect"], f"/catalog/meli/new/{draft.pk}/?prospect={self.prospect.pk}")
        draft.refresh_from_db()
        self.assertEqual(draft.category_id, "MLA5555")
        self.assertEqual(draft.attributes, {"PERFUME_NAME": "Mío"})
        self.assertIn("MLA5555", self.meli.categories_read)
        other = shared_seller("Other", user_id="777")
        theirs = prospect_for(other, external_id="MLAU999", category_id="MLA7777")
        self.assertEqual(self.client.post(f"/catalog/meli/new/{draft.pk}/scraped/", {"prospect": theirs.pk}).status_code, 404)
        draft.refresh_from_db()
        self.assertEqual(draft.category_id, "MLA5555")


class ScrapedCategoryTests(PerfumeDraftCase):
    def store(self, entry):
        from .prospects import store

        with self.captureOnCommitCallbacks(execute=True):
            return store(self.organization, entry)[0][0]

    def test_the_scrapers_category_is_used_and_its_path_read(self):
        prospect = self.store({**SCRAPED[0], "categoryId": "mla5555"})
        prospect.refresh_from_db()
        self.assertEqual((prospect.category_id, prospect.category_path), ("MLA5555", "Belleza y Cuidado Personal > Perfumes"))
        self.assertEqual(self.meli.discovered, [])

    def test_without_one_mercado_libre_guesses_from_the_title_and_the_tab_shows_it(self):
        prospect = self.store(SCRAPED[0])
        prospect.refresh_from_db()
        self.assertEqual(self.meli.discovered, [SCRAPED[0]["title"]])
        self.assertEqual(prospect.category_id, "MLA9999")
        page = self.client.get(PAGE_URL)
        self.assertContains(page, '<span class="mono">MLA9999</span> <span class="muted">· Belleza y Cuidado Personal &gt; Perfumes</span>', html=False)
        # The same page again keeps it; a new title looks it up again.
        self.store(SCRAPED[0])
        self.assertEqual(len(self.meli.discovered), 1)
        self.store({**SCRAPED[0], "title": "Pestañas pelo a pelo"})
        self.assertEqual(self.meli.discovered[-1], "Pestañas pelo a pelo")

    def test_a_failed_lookup_shows_why_and_can_be_tried_again(self):
        with patch("meli.drafts._token", side_effect=OSError("down")):
            prospect = self.store(SCRAPED[0])
        prospect.refresh_from_db()
        self.assertEqual(prospect.category_error, "Couldn't look it up: Mercado Libre didn't answer. Try again.")
        self.assertContains(self.client.get(PAGE_URL), "Try again")
        self.client.post("/catalog/meli/new/", {"action": "scraped_category", "prospect": prospect.pk})
        prospect.refresh_from_db()
        self.assertEqual((prospect.category_id, prospect.category_error), ("MLA9999", ""))


UNITS = {"id": "UNITS_PER_PACK", "name": "Cantidad de envases", "tags": {}}
DRAFT_FORM = {"quantity": "1", "category_id": "MLA1271", "use_defaults": "on",
              "condition": "new", "warranty": "seller", "warranty_days": "30"}


@override_settings(MISTRAL_API_KEY="")
class LeaveOutPriceAndCompetitorTests(PerfumeDraftCase):
    def setUp(self):
        super().setUp()
        self.meli.category_attributes = [*PERFUME_ATTRIBUTES, NAME, GENDER, UNITS]
        self.product.attributes = {**self.product.attributes, "UNITS_PER_PACK": 1}
        self.product.save()

    def test_a_left_out_attribute_isnt_sent_and_can_leave_the_product(self):
        from .drafts import build_item, defaults_for

        self.create()
        draft = self.draft()
        page = self.client.get(f"/catalog/meli/new/{draft.pk}/")
        self.assertContains(page, 'name="attr_omit" value="UNITS_PER_PACK"', html=False)
        # Required ones can't be left out, and say so in words.
        self.assertNotContains(page, 'name="attr_omit" value="PERFUME_TYPE"', html=False)
        self.assertContains(page, '<span class="req-tag">Required</span>', html=False)
        self.assertIn({"id": "UNITS_PER_PACK", "value_name": "1"}, build_item(draft, defaults_for(self.organization))["attributes"])
        self.client.post(f"/catalog/meli/new/{draft.pk}/", {
            **DRAFT_FORM, "price": "15999", "attr_key": ["UNITS_PER_PACK"], "attr_value": ["1"],
            "attr_omit": ["UNITS_PER_PACK"], "save_to_product": "on",
        })
        draft.refresh_from_db()
        self.assertIsNone(draft.attributes["UNITS_PER_PACK"])
        sent = {attribute["id"] for attribute in build_item(draft, defaults_for(self.organization))["attributes"]}
        self.assertNotIn("UNITS_PER_PACK", sent)
        self.product.refresh_from_db()
        self.assertNotIn("UNITS_PER_PACK", self.product.attributes)
        page = self.client.get(f"/catalog/meli/new/{draft.pk}/")
        self.assertContains(page, 'name="attr_omit" value="UNITS_PER_PACK" checked', html=False)

    def test_the_scraped_price_is_suggested_and_the_competitor_shown(self):
        prospect = prospect_for(self.organization, price=Decimal("15000"), seller="BEAUTYBODYS",
                                url="https://www.mercadolibre.com.ar/x/up/MLAU3250631132", product=self.product)
        self.client.post(f"/catalog/meli/new/scraped/{prospect.pk}/")
        draft = self.draft()
        self.assertEqual(draft.price, Decimal("15000"))
        page = self.client.get(f"/catalog/meli/new/{draft.pk}/")
        self.assertContains(page, "Competitor:")
        self.assertContains(page, "BEAUTYBODYS · ARS 15000")
        actions = self.client.get(f"/catalog/meli/products/{self.product.pk}/")
        self.assertContains(actions, "Competitors on Mercado Libre")
        self.assertContains(actions, 'href="https://www.mercadolibre.com.ar/x/up/MLAU3250631132"', html=False)
        other = shared_seller("Other", user_id="777")
        prospect_for(other, external_id="MLAU999", title="Theirs", product=None)
        self.assertNotContains(self.client.get(f"/catalog/meli/products/{self.product.pk}/"), "Theirs")


@override_settings(MISTRAL_API_KEY="")
class FastPublishTests(PerfumeDraftCase):
    def setUp(self):
        super().setUp()
        self.meli.category_attributes = [*PERFUME_ATTRIBUTES, NAME, GENDER]
        from django.utils import timezone

        self.prospect = prospect_for(
            self.organization, price=Decimal("15000"), category_id="MLA1271", category_checked_at=timezone.now(),
            payload={**SCRAPED[0], "images": {"gallery": ["https://http2.mlstatic.com/a.webp", "https://http2.mlstatic.com/b.jpg"]}},
        )

    def fast(self):
        return self.client.post(f"/catalog/meli/new/scraped/{self.prospect.pk}/fast/")

    def test_fast_publishing_posts_without_a_product_and_links_one_later(self):
        self.assertContains(self.client.get(PAGE_URL), "Fast publish")
        response = self.fast()
        draft = self.draft()
        self.assertRedirects(response, f"/catalog/meli/new/{draft.pk}/?prospect={self.prospect.pk}", fetch_redirect_response=False)
        self.assertEqual((draft.kind, draft.product, draft.combo), (ListingDraft.Kind.FAST, None, None))
        self.assertEqual((draft.title, draft.price, draft.category_id), (SCRAPED[0]["title"], Decimal("15000"), "MLA1271"))
        self.assertEqual(draft.pictures, ["https://http2.mlstatic.com/a.webp", "https://http2.mlstatic.com/b.jpg"])
        self.assertEqual(draft.error, "Mercado Libre needs the package weight and size: fill in Package below, then Save and check.")
        page = self.client.get(f"/catalog/meli/new/{draft.pk}/")
        self.assertContains(page, "Fast listing: not linked to a Vimple product")
        self.assertContains(page, "which are another seller's")
        self.prospect.refresh_from_db()
        self.assertEqual((self.prospect.status, self.prospect.draft), (MeliListingProspect.Status.DRAFTED, draft))

        # More than the Depósito holds (3): no product, so no cap.
        self.client.post(f"/catalog/meli/new/{draft.pk}/", {
            **DRAFT_FORM, "price": "14500", "quantity": "5", "title": "Kit pestañas descartables",
            "pictures_text": "https://img.example/mine.jpg\nhttps://http2.mlstatic.com/b.jpg",
            "weight_g": "120", "length_cm": "15", "width_cm": "10", "height_cm": "5",
            "attr_key": ["BRAND", "PERFUME_TYPE", "UNIT_VOLUME", "PERFUME_NAME", "GENDER"],
            "attr_value": ["City Girl", "Eau de parfum", "30 mL", "Kit", "Mujer"],
            # No product description to start from: the seller writes one.
            "desc_present": "1", "desc_part": ["text"], "desc_text": ["Kit de insumos descartables para pestañas."],
        })
        draft.refresh_from_db()
        self.assertEqual(draft.status, ListingDraft.Status.READY, draft.error)
        body = self.meli.validated[-1]
        sent = {attribute["id"]: attribute for attribute in body["attributes"]}
        self.assertNotIn("SELLER_SKU", sent)
        self.assertEqual(sent["BRAND"]["value_name"], "City Girl")
        self.assertEqual(sent["SELLER_PACKAGE_WEIGHT"]["value_name"], "120 g")
        self.assertEqual(body["pictures"], [{"source": "https://img.example/mine.jpg"}, {"source": "https://http2.mlstatic.com/b.jpg"}])
        self.assertEqual((body["available_quantity"], body["title"]), (5, "Kit pestañas descartables"))

        self.client.post("/catalog/meli/new/", {"action": "post", "draft": [draft.pk]})
        draft.refresh_from_db()
        self.assertEqual((draft.status, draft.item_id), (ListingDraft.Status.PUBLISHED, "MLA900"), draft.error)
        self.assertIsNone(MeliListing.objects.get(organization=self.organization, item_id="MLA900").product)

        # Linked later: its SKU goes to Mercado Libre and the sync links the listing.
        self.client.post(PAGE_URL, {"action": "scraped_link", "prospect": self.prospect.pk, "sku": self.product.sku})
        self.assertEqual(self.meli.skus_set, [("MLA900", self.product.sku)])
        self.assertEqual(MeliListing.objects.get(organization=self.organization, item_id="MLA900").product, self.product)

    def test_only_owners_and_admins_and_only_this_organizations_rows(self):
        other = shared_seller("Other", user_id="777")
        theirs = prospect_for(other, external_id="MLAU999")
        self.assertEqual(self.client.post(f"/catalog/meli/new/scraped/{theirs.pk}/fast/").status_code, 404)
        self.client.force_login(member(self.organization, role=Membership.Role.CONTRIBUTOR, username="bea"))
        self.assertEqual(self.fast().status_code, 403)
        self.assertFalse(ListingDraft.objects.exists())
