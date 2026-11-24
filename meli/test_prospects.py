import json
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings

from accounts.models import Membership, Organization, OrganizationApiKey
from catalog.test_suggest import FakeMistral
from catalog.test_views import member
from catalog.tests import product

from .models import ListingDraft, MeliListingProspect
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

        fake = FakeMistral({"values": {"PERFUME_NAME": "885", "GENDER": "Mujer"}, "dropped": {}})
        with patch("vimple.ai.urlopen", side_effect=fake):
            copied = self.client.post(f"/catalog/meli/new/{draft.pk}/copy/", {"prospect": self.prospect.pk})
        sent = json.loads(fake.asked[0]["messages"][1]["content"])
        # Matched by name; never the other seller's brand, and nothing the category doesn't have.
        self.assertEqual(sorted(item["id"] for item in sent["source_listing"]["attributes"]), ["GENDER", "PERFUME_NAME"])
        self.assertContains(copied, "Copied 2 values")

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
        copied = self.client.post(f"/catalog/meli/new/{draft.pk}/copy/", {"prospect": empty.pk})
        self.assertContains(copied, "Nothing to copy from")
        self.assertNotContains(copied, "Copied 0 values")
