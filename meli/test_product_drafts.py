import json
from decimal import Decimal
from unittest.mock import patch

from django.test import override_settings

from accounts.models import Membership
from catalog.images import set_images
from catalog.test_suggest import FakeMistral
from catalog.test_views import member
from catalog.tests import product
from inventory import ledger

from .drafts import build_item, defaults_for, missing_attributes, schema_of, uses_user_products
from .models import ListingDraft, MeliListing
from .test_drafts import DraftTestCase
from .tests import shared_seller

PERFUME_ATTRIBUTES = [
    {"id": "BRAND", "name": "Marca", "tags": {"required": True}},
    {"id": "PERFUME_TYPE", "name": "Tipo de perfume", "tags": {"required": True}, "value_type": "list",
     "values": [{"id": "1", "name": "Eau de parfum"}, {"id": "2", "name": "Eau de toilette"}]},
    {"id": "UNIT_VOLUME", "name": "Volumen de la unidad", "tags": {"required": True}, "value_type": "number_unit"},
    {"id": "OLFACTORY_NOTES", "name": "Notas olfativas", "tags": {}},
    {"id": "155_98_RESOLUTION_FILE_NUMBER", "name": "Legajo", "tags": {"required": True}},
    {"id": "SHIPMENT_PACKING", "name": "Embalaje", "tags": {"read_only": True, "required": True}},
    {"id": "GTIN", "name": "Código universal de producto", "tags": {"conditional_required": True}},
    {"id": "EMPTY_GTIN_REASON", "name": "Motivo de GTIN vacío", "tags": {}, "value_type": "list",
     "values": [{"id": "17055160", "name": "El producto no tiene código registrado"}, {"id": "17055161", "name": "Otra razón"}]},
]


class ProductDraftTests(DraftTestCase):
    def setUp(self):
        super().setUp()
        self.meli.category_attributes = PERFUME_ATTRIBUTES
        self.product.list_price = Decimal("15999")
        self.product.gtin = "7798123456789"
        self.product.meli_category_id = "MLA1271"
        self.product.attributes = {"UNIT_VOLUME": "30 mL", "OLFACTORY_NOTES": ["Amaderado", "Cítrico"], "FOR_US_ONLY": "x"}
        self.product.weight_g = 310
        self.product.height_cm = Decimal("17.0")
        self.product.width_cm = Decimal("7.5")
        self.product.length_cm = Decimal("3.0")
        self.product.save()
        model = self.product.product_model
        model.description = "Eau de parfum amaderada de 30 ml."
        model.save()
        set_images(self.product, ["https://img.example/a.jpg", "https://img.example/b.jpg"])

    def create(self, kind="product"):
        return self.client.post(f"/catalog/meli/products/{self.product.pk}/draft/", {"kind": kind})

    def test_the_product_page_offers_the_buttons_when_it_can_be_listed(self):
        actions = self.client.get(f"/catalog/meli/products/{self.product.pk}/")
        self.assertContains(actions, "Create Mercado Libre listing")
        self.assertNotContains(actions, "List in its catalog")
        self.product.meli_catalog_product_id = "MLA46233297"
        self.product.save()
        self.assertContains(self.client.get(f"/catalog/meli/products/{self.product.pk}/"), "List in its catalog (MLA46233297)")
        ledger.record(self.organization, self.product, ledger.Kind.DAMAGE, ledger.OWN, -3)
        actions = self.client.get(f"/catalog/meli/products/{self.product.pk}/")
        self.assertContains(actions, "No stock in the Depósito to list.")
        self.assertEqual(self.create().status_code, 400)
        self.assertFalse(ListingDraft.objects.exists())

    def test_missing_required_attributes_stop_the_check_before_mercado_libre_validates(self):
        response = self.create()
        draft = self.draft()
        self.assertRedirects(response, f"/catalog/meli/new/{draft.pk}/")
        self.assertEqual(draft.kind, ListingDraft.Kind.PRODUCT)
        self.assertEqual(draft.status, ListingDraft.Status.NEEDS_FIX)
        self.assertEqual(draft.error, "Missing: Tipo de perfume. Add them below.")
        self.assertEqual(self.meli.validated, [])
        page = self.client.get(f"/catalog/meli/new/{draft.pk}/")
        self.assertContains(page, '<strong>Tipo de perfume</strong> <span class="mono">PERFUME_TYPE</span>: one of Eau de parfum, Eau de toilette', html=False)
        self.assertContains(page, 'value="PERFUME_TYPE"')

    def test_the_body_sends_pictures_brand_model_and_only_attributes_the_category_knows(self):
        self.create()
        draft = self.draft()
        body = build_item(draft, defaults_for(self.organization))
        self.assertEqual(body["title"], self.product.title)
        self.assertNotIn("family_name", body)
        self.assertEqual(body["pictures"], [{"source": "https://img.example/a.jpg"}, {"source": "https://img.example/b.jpg"}])
        self.assertEqual(body["category_id"], "MLA1271")
        self.assertNotIn("catalog_product_id", body)
        sent = {attribute["id"]: attribute.get("value_name") for attribute in body["attributes"]}
        self.assertEqual(sent["BRAND"], self.product.product_model.brand.name)
        self.assertEqual(sent["MODEL"], self.product.product_model.name)
        self.assertEqual(sent["GTIN"], "7798123456789")
        self.assertEqual(sent["UNIT_VOLUME"], "30 mL")
        self.assertEqual(sent["OLFACTORY_NOTES"], "Amaderado, Cítrico")
        self.assertEqual(sent["SELLER_SKU"], self.product.sku)
        self.assertNotIn("FOR_US_ONLY", sent)
        self.assertEqual(
            [sent[key] for key in ("SELLER_PACKAGE_HEIGHT", "SELLER_PACKAGE_WIDTH", "SELLER_PACKAGE_LENGTH", "SELLER_PACKAGE_WEIGHT")],
            ["17 cm", "7.5 cm", "3 cm", "310 g"],
        )
        self.assertEqual([item["id"] for item in missing_attributes(draft, body)], ["PERFUME_TYPE"])

    def test_a_user_products_seller_sends_family_name_instead_of_title(self):
        from .models import SellerProfile

        self.assertFalse(uses_user_products(self.organization))
        SellerProfile.objects.create(organization=self.organization, payload={"tags": ["normal", "user_product_seller"]})
        self.assertTrue(uses_user_products(self.organization))
        self.create()
        body = build_item(self.draft(), defaults_for(self.organization), uses_user_products(self.organization))
        self.assertEqual(body["family_name"], self.product.title)
        self.assertNotIn("title", body)

    def test_without_the_package_size_the_check_stops_before_mercado_libre(self):
        self.product.width_cm = None
        self.product.save()
        self.create()
        draft = self.draft()
        self.assertEqual(draft.status, ListingDraft.Status.NEEDS_FIX)
        self.assertIn("package weight and size", draft.error)
        self.assertEqual(self.meli.validated, [])

    def test_without_a_gtin_the_listing_goes_out_as_brand_generic(self):
        self.product.gtin = ""
        self.product.save()
        self.create()
        draft = self.draft()
        page = self.client.get(f"/catalog/meli/new/{draft.pk}/")
        self.assertContains(page, "listed as brand <strong>Generic</strong>", html=False)
        self.client.post(
            f"/catalog/meli/new/{draft.pk}/",
            {
                "price": "15999", "quantity": "1", "category_id": "MLA1271", "use_defaults": "on",
                "condition": "new", "warranty": "seller", "warranty_days": "30",
                "attr_key": ["PERFUME_TYPE"], "attr_value": ["Eau de parfum"],
            },
        )
        draft.refresh_from_db()
        self.assertEqual(draft.status, ListingDraft.Status.READY, draft.error)
        sent = self.meli.validated[-1]["attributes"]
        self.assertIn({"id": "BRAND", "value_id": "35977846"}, sent)
        ids = [attribute["id"] for attribute in sent]
        self.assertNotIn("GTIN", ids)
        self.assertNotIn("EMPTY_GTIN_REASON", ids)
        # A brand typed on the draft wins.
        draft.attributes = {"PERFUME_TYPE": "Eau de parfum", "BRAND": "Onlyou"}
        body = build_item(draft, defaults_for(self.organization))
        self.assertIn({"id": "BRAND", "value_name": "Onlyou"}, body["attributes"])

    def test_a_typed_gtin_reason_hidden_or_unlisted_still_goes_by_its_id(self):
        self.product.gtin = ""
        self.product.save()
        hidden = [dict(attribute) for attribute in PERFUME_ATTRIBUTES]
        for attribute in hidden:
            if attribute["id"] == "EMPTY_GTIN_REASON":
                attribute["tags"] = {"hidden": True, "required": True}
        self.meli.category_attributes = hidden
        self.create()
        draft = self.draft()
        reason = next(item for item in draft.category_schema if item["id"] == "EMPTY_GTIN_REASON")
        self.assertEqual((reason["required"], reason["value_ids"][-1]), (False, "17055161"))
        self.assertNotIn("Motivo", draft.error)
        # Not in the category at all, typed with other capitals: still Mercado Libre's id.
        draft.category_schema = [item for item in draft.category_schema if item["id"] != "EMPTY_GTIN_REASON"]
        draft.attributes = {"EMPTY_GTIN_REASON": "Otra Razón"}
        body = build_item(draft, defaults_for(self.organization))
        self.assertIn({"id": "EMPTY_GTIN_REASON", "value_id": "17055161"}, body["attributes"])

    def test_a_unit_without_a_number_stops_the_check(self):
        self.create()
        draft = self.draft()
        draft.attributes = {"PERFUME_TYPE": "Eau de parfum", "UNIT_VOLUME": "ML"}
        draft.status = ListingDraft.Status.CHECKING
        draft.save()
        from .drafts import check

        check(self.organization, [draft.pk])
        draft.refresh_from_db()
        self.assertEqual(draft.status, ListingDraft.Status.NEEDS_FIX)
        self.assertEqual(draft.error, "Volumen de la unidad (UNIT_VOLUME): “ML” needs a number and a unit, like 30 mL.")
        self.assertEqual(self.meli.validated, [])
        for good in ("30 mL", "30ml", "7,5 cm"):
            draft.attributes = {"PERFUME_TYPE": "Eau de parfum", "UNIT_VOLUME": good}
            body = build_item(draft, defaults_for(self.organization))
            from .drafts import invalid_values

            self.assertEqual(invalid_values(draft, body), [], good)

    def test_with_a_gtin_no_reason_is_sent(self):
        self.create()
        body = build_item(self.draft(), defaults_for(self.organization))
        ids = [attribute["id"] for attribute in body["attributes"]]
        self.assertIn("GTIN", ids)
        self.assertNotIn("EMPTY_GTIN_REASON", ids)

    def test_a_value_not_on_mercado_libres_list_stops_the_check_with_the_choices(self):
        self.product.gtin = ""
        self.product.save()
        self.create()
        draft = self.draft()
        self.client.post(
            f"/catalog/meli/new/{draft.pk}/",
            {
                "price": "15999", "quantity": "1", "category_id": "MLA1271", "use_defaults": "on",
                "condition": "new", "warranty": "seller", "warranty_days": "30",
                "attr_key": ["PERFUME_TYPE", "EMPTY_GTIN_REASON"], "attr_value": ["eau de parfum", "Other"],
            },
        )
        draft.refresh_from_db()
        self.assertEqual(draft.status, ListingDraft.Status.NEEDS_FIX)
        self.assertEqual(
            draft.error,
            "Motivo de GTIN vacío (EMPTY_GTIN_REASON): “Other” isn't one of El producto no tiene código registrado, Otra razón.",
        )
        self.assertEqual(self.meli.validated, [])

    def test_read_only_attributes_are_never_asked_for(self):
        ids = [item["id"] for item in schema_of(PERFUME_ATTRIBUTES)]
        self.assertNotIn("SHIPMENT_PACKING", ids)

    def test_adding_the_missing_attribute_checks_posts_and_adds_the_description(self):
        self.create()
        draft = self.draft()
        response = self.client.post(
            f"/catalog/meli/new/{draft.pk}/",
            {
                "price": "15999", "quantity": "1", "category_id": "MLA1271", "use_defaults": "on",
                "condition": "new", "warranty": "seller", "warranty_days": "30",
                "attr_key": ["PERFUME_TYPE"], "attr_value": ["Eau de parfum"],
            },
        )
        self.assertRedirects(response, "/catalog/meli/new/")
        draft.refresh_from_db()
        self.assertEqual(draft.status, ListingDraft.Status.READY, draft.error)
        self.assertEqual(draft.attributes, {"PERFUME_TYPE": "Eau de parfum"})
        # A value on Mercado Libre's list goes by its id.
        self.assertIn({"id": "PERFUME_TYPE", "value_id": "1"}, self.meli.validated[-1]["attributes"])
        self.client.post("/catalog/meli/new/", {"action": "post_all"})
        draft.refresh_from_db()
        self.assertEqual(draft.status, ListingDraft.Status.PUBLISHED, draft.error)
        self.assertEqual(self.meli.descriptions, [{"plain_text": "Eau de parfum amaderada de 30 ml."}])
        self.assertEqual(MeliListing.objects.get(item_id="MLA900").product, self.product)

    def test_a_refused_description_is_noted_but_the_listing_stays_posted(self):
        self.create()
        draft = self.draft()
        draft.attributes = {"PERFUME_TYPE": "Eau de parfum"}
        draft.status = ListingDraft.Status.READY
        draft.save()
        self.meli.refuse_description = True
        self.meli.refuse = ""
        self.client.post("/catalog/meli/new/", {"action": "post_all"})
        draft.refresh_from_db()
        self.assertEqual(draft.status, ListingDraft.Status.PUBLISHED)
        self.assertIn("Posted without its description", draft.error)

    def test_without_a_category_mercado_libre_suggests_one_from_the_title(self):
        self.product.meli_category_id = ""
        self.product.save()
        self.create()
        self.assertEqual(self.draft().category_id, "MLA9999")
        self.assertEqual(self.meli.categories_read, ["MLA9999"])

    def test_without_a_list_price_the_draft_asks_for_one(self):
        self.product.list_price = None
        self.product.save()
        self.create()
        draft = self.draft()
        self.assertEqual((draft.status, draft.error), (ListingDraft.Status.NEEDS_FIX, "Set a price, then Save and check."))
        self.assertEqual(self.meli.validated, [])

    def test_list_in_its_catalog_makes_a_catalog_draft(self):
        self.product.meli_catalog_product_id = "MLA46233297"
        self.product.save()
        self.meli.category_attributes = [{"id": "BRAND"}]
        with patch("meli.test_drafts.CATALOG", "MLA46233297"):
            self.create(kind="catalog")
        draft = self.draft()
        self.assertEqual((draft.kind, draft.catalog_product_id), (ListingDraft.Kind.CATALOG, "MLA46233297"))

    @override_settings(MISTRAL_API_KEY="test-key")
    def test_fill_with_ai_stays_after_a_mercado_libre_refusal_and_asks_about_what_it_named(self):
        self.create()
        draft = self.draft()
        draft.attributes = {"PERFUME_TYPE": "Eau de parfum", "UNIT_VOLUME": "30 mL"}
        draft.status = ListingDraft.Status.NEEDS_FIX
        draft.error = "Attribute UNIT_VOLUME with value ML is required and was omitted."
        draft.error_details = {
            "call": "POST /items/validate",
            "response": {"cause": [
                {"type": "error", "code": "item.attributes.normalizable.invalid",
                 "message": "Attribute UNIT_VOLUME with value ML is required and was omitted."},
                {"type": "warning", "code": "x", "message": "PERFUME_TYPE is fine"},
            ]},
        }
        draft.save()
        page = self.client.get(f"/catalog/meli/new/{draft.pk}/")
        self.assertContains(page, "Fill with AI")
        fake = FakeMistral({"UNIT_VOLUME": "30 mL"})
        with patch("vimple.ai.urlopen", side_effect=fake):
            self.client.post(
                f"/catalog/meli/new/{draft.pk}/ai/",
                {"attr_key": ["PERFUME_TYPE", "UNIT_VOLUME"], "attr_value": ["Eau de parfum", "30 mL"]},
            )
        asked = json.loads(fake.asked[0]["messages"][1]["content"])["attributes_to_fill"]
        self.assertEqual([item["id"] for item in asked], ["UNIT_VOLUME"])
        self.assertEqual(asked[0]["current_value_refused"], "30 mL")

    @override_settings(MISTRAL_API_KEY="test-key")
    def test_with_nothing_specific_fill_with_ai_suggests_the_required_attributes_again(self):
        self.create()
        draft = self.draft()
        draft.attributes = {"PERFUME_TYPE": "Eau de parfum"}
        draft.save()
        fake = FakeMistral({})
        with patch("vimple.ai.urlopen", side_effect=fake):
            self.client.post(f"/catalog/meli/new/{draft.pk}/ai/", {"attr_key": ["PERFUME_TYPE"], "attr_value": ["Eau de parfum"]})
        asked = [item["id"] for item in json.loads(fake.asked[0]["messages"][1]["content"])["attributes_to_fill"]]
        self.assertEqual(asked, ["PERFUME_TYPE", "UNIT_VOLUME"])

    @override_settings(MISTRAL_API_KEY="test-key")
    def test_fill_with_ai_also_fixes_a_refused_value_from_the_list(self):
        self.product.gtin = ""
        self.product.save()
        self.create()
        draft = self.draft()
        draft.attributes = {"PERFUME_TYPE": "Eau de parfum", "EMPTY_GTIN_REASON": "Other"}
        draft.save()
        page = self.client.get(f"/catalog/meli/new/{draft.pk}/")
        self.assertContains(page, "“Other” isn't allowed; one of El producto no tiene código registrado, Otra razón")
        self.assertContains(page, "Fill with AI")
        fake = FakeMistral({"EMPTY_GTIN_REASON": "otra razón"})
        with patch("vimple.ai.urlopen", side_effect=fake):
            response = self.client.post(
                f"/catalog/meli/new/{draft.pk}/ai/",
                {"attr_key": ["PERFUME_TYPE", "EMPTY_GTIN_REASON"], "attr_value": ["Eau de parfum", "Other"]},
            )
        asked = json.loads(fake.asked[0]["messages"][1]["content"])["attributes_to_fill"]
        self.assertEqual(
            asked,
            [{"id": "EMPTY_GTIN_REASON", "name": "Motivo de GTIN vacío", "value_type": "list",
              "allowed_values": ["El producto no tiene código registrado", "Otra razón"], "current_value_refused": "Other"}],
        )
        self.assertContains(response, 'value="Otra razón"')
        self.assertIn("allowed_values", fake.asked[0]["messages"][0]["content"])

    @override_settings(MISTRAL_API_KEY="test-key")
    def test_fill_with_ai_suggests_only_allowed_values(self):
        self.create()
        draft = self.draft()
        page = self.client.get(f"/catalog/meli/new/{draft.pk}/")
        self.assertContains(page, "Fill with AI")
        fake = FakeMistral({"PERFUME_TYPE": "eau de parfum"})
        with patch("vimple.ai.urlopen", side_effect=fake):
            response = self.client.post(f"/catalog/meli/new/{draft.pk}/ai/", {"attr_key": [""], "attr_value": [""]})
        self.assertContains(response, 'value="Eau de parfum"')
        asked = json.loads(fake.asked[0]["messages"][1]["content"])
        self.assertEqual(asked["attributes_to_fill"][0]["allowed_values"], ["Eau de parfum", "Eau de toilette"])
        draft.refresh_from_db()
        self.assertEqual(draft.attributes, {})
        with patch("vimple.ai.urlopen", side_effect=FakeMistral({"PERFUME_TYPE": "Perfume de verano"})):
            response = self.client.post(f"/catalog/meli/new/{draft.pk}/ai/", {})
        self.assertNotContains(response, "Perfume de verano")
        self.assertContains(response, "No confident values")

    def test_other_organizations_and_contributors_are_kept_out(self):
        other = shared_seller("Other", user_id="777")
        theirs = product(other, variant="75ML")
        self.assertEqual(self.client.post(f"/catalog/meli/products/{theirs.pk}/draft/").status_code, 404)
        self.assertEqual(self.client.get(f"/catalog/meli/products/{theirs.pk}/").status_code, 404)
        self.create()
        draft = self.draft()
        self.client.force_login(member(self.organization, role=Membership.Role.CONTRIBUTOR, username="bea"))
        self.assertEqual(self.create().status_code, 403)
        self.assertEqual(self.client.post(f"/catalog/meli/new/{draft.pk}/ai/", {}).status_code, 403)
        self.assertNotContains(self.client.get(f"/catalog/meli/products/{self.product.pk}/"), "Create Mercado Libre listing")
