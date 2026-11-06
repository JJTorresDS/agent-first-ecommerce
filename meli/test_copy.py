import json
from unittest.mock import patch

from django.test import override_settings

from accounts.models import Membership
from catalog.test_suggest import FakeMistral
from catalog.test_views import member
from inventory.tests import listing

from .models import ListingTemplate
from .test_product_drafts import PERFUME_ATTRIBUTES, PerfumeDraftCase
from .tests import shared_seller

NAME = {"id": "PERFUME_NAME", "name": "Nombre del perfume", "tags": {"required": True}}
GENDER = {"id": "GENDER", "name": "Género", "tags": {"required": True}, "value_type": "list",
          "values": [{"id": "1", "name": "Hombre"}, {"id": "2", "name": "Mujer"}]}
SOURCE = [
    {"id": "BRAND", "name": "Marca", "value_name": "Lattafa"},
    {"id": "SELLER_SKU", "name": "SKU", "value_name": "PERF-LATT-900-100ML"},
    {"id": "PERFUME_NAME", "name": "Nombre del perfume", "value_name": "Asad"},
    {"id": "GENDER", "name": "Género", "value_name": "Hombre"},
    {"id": "PERFUME_TYPE", "name": "Tipo de perfume", "value_name": "Eau de parfum"},
    {"id": "UNIT_VOLUME", "name": "Volumen", "value_name": "100 mL"},
]


@override_settings(MISTRAL_API_KEY="test-key")
class CopyFromListingTests(PerfumeDraftCase):
    def setUp(self):
        super().setUp()
        self.meli.category_attributes = [*PERFUME_ATTRIBUTES, NAME, GENDER]
        self.create()
        self.draft_row = self.draft()
        self.source = listing(self.organization, "MLA800", title="Perfume Asad 100 ml", category_id="MLA1271",
                              payload={"attributes": SOURCE})

    def copy(self, source=None, answer=None, **post):
        fake = FakeMistral(answer or {
            "values": {"PERFUME_NAME": "885", "GENDER": "hombre", "PERFUME_TYPE": "Perfume de verano"},
            "dropped": {},
        })
        with patch("vimple.ai.urlopen", side_effect=fake):
            response = self.client.post(
                f"/catalog/meli/new/{self.draft_row.pk}/copy/", {"listing": (source or self.source).pk, **post}
            )
        return response, fake

    def test_the_dialog_lists_the_categorys_listings_with_the_template_first(self):
        other = listing(self.organization, "MLA801", title="Perfume Yara", category_id="MLA1271")
        listing(self.organization, "MLA802", title="Lampara", category_id="MLA39965")
        ListingTemplate.objects.create(organization=self.organization, category_id="MLA1271", item_id="MLA801", state="ready")
        page = self.client.get(f"/catalog/meli/new/{self.draft_row.pk}/").content.decode()
        self.assertIn("Copy from a listing", page)
        self.assertLess(page.index(other.title), page.index(self.source.title))
        self.assertNotIn("Lampara", page)

    def test_ai_gets_the_other_listings_values_for_empty_fields_only_and_off_list_values_are_dropped(self):
        response, fake = self.copy()
        sent = json.loads(fake.asked[0]["messages"][1]["content"])
        # Not the other item's brand or SKU, and not the volume: the product already has one.
        self.assertEqual([item["id"] for item in sent["source_listing"]["attributes"]], ["PERFUME_NAME", "GENDER", "PERFUME_TYPE"])
        self.assertEqual(sent["target_product"]["attributes"]["UNIT_VOLUME"], "30 mL")
        self.assertEqual(sent["attribute_rules"]["GENDER"]["allowed_values"], ["Hombre", "Mujer"])
        self.assertNotIn("15999", json.dumps(sent))
        self.assertContains(response, "Copied 2 values from “Perfume Asad 100 ml”")
        self.assertContains(response, '<option value="Hombre" selected>Hombre</option>', html=False)
        self.assertContains(response, 'id="ga-PERFUME_NAME" value="885"', html=False)
        self.assertContains(response, "<strong>Tipo de perfume</strong>: not one of Mercado Libre&#x27;s values", html=False)
        self.draft_row.refresh_from_db()
        self.assertEqual(self.draft_row.attributes, {})
        # Nothing went to Mercado Libre: the listing is read as synced.
        self.assertNotIn("/items/MLA800", [path for _method, path in self.meli.calls])

    def test_a_typed_value_is_never_replaced(self):
        _response, fake = self.copy(attr_key=["PERFUME_NAME"], attr_value=["Mío"])
        sent = json.loads(fake.asked[0]["messages"][1]["content"])
        self.assertNotIn("PERFUME_NAME", [item["id"] for item in sent["source_listing"]["attributes"]])

    def test_saving_can_keep_the_values_on_the_product(self):
        data = {
            "price": "15999", "quantity": "1", "category_id": "MLA1271", "use_defaults": "on",
            "condition": "new", "warranty": "seller", "warranty_days": "30",
            "attr_key": ["PERFUME_NAME", "GENDER", "EMPTY_GTIN_REASON"], "attr_value": ["885", "Hombre", "Otra razón"],
        }
        self.client.post(f"/catalog/meli/new/{self.draft_row.pk}/", data)
        self.product.refresh_from_db()
        self.assertNotIn("PERFUME_NAME", self.product.attributes)
        self.client.post(f"/catalog/meli/new/{self.draft_row.pk}/", {**data, "save_to_product": "on"})
        self.product.refresh_from_db()
        self.assertEqual((self.product.attributes["PERFUME_NAME"], self.product.attributes["GENDER"]), (885, "Hombre"))
        self.assertNotIn("EMPTY_GTIN_REASON", self.product.attributes)
        self.assertEqual(self.product.attributes["UNIT_VOLUME"], "30 mL")

    def test_other_organizations_listings_and_contributors_are_kept_out(self):
        other = shared_seller("Other", user_id="777")
        theirs = listing(other, "MLA777", title="Theirs", category_id="MLA1271", payload={"attributes": SOURCE})
        response, fake = self.copy(source=theirs)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(fake.asked, [])
        self.assertNotIn("Theirs", self.client.get(f"/catalog/meli/new/{self.draft_row.pk}/").content.decode())
        self.client.force_login(member(self.organization, role=Membership.Role.CONTRIBUTOR, username="bea"))
        self.assertEqual(self.copy()[0].status_code, 403)

    def test_a_template_draft_offers_no_copy(self):
        self.draft_row.template = True
        self.draft_row.save()
        self.assertNotContains(self.client.get(f"/catalog/meli/new/{self.draft_row.pk}/"), "Copy from a listing")
