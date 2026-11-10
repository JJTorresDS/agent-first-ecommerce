import copy
import json
from decimal import Decimal
from unittest.mock import patch

from django.test import override_settings

from accounts.models import Membership
from catalog.models import MasterProduct
from catalog.test_suggest import FakeMistral
from catalog.test_views import member
from catalog.tests import product

from .drafts import build_item, defaults_for, package_from_fix
from .models import ListingDraft
from .test_drafts import CATALOG, DraftTestCase, FakeSellFlow
from .tests import shared_seller


@override_settings(MISTRAL_API_KEY="test-key")
class AiFixTests(DraftTestCase):
    def setUp(self):
        super().setUp()
        self.meli.refuse = "The packaging attributes are too small for the product dimensions"
        self.add()
        self.refused = self.draft()
        self.meli.refuse = ""

    def suggestion(self, **changes):
        """What Mistral answers: the body it was sent, with changes, and one reason per change."""
        body = copy.deepcopy(self.refused.error_details["sent"])
        body.update(changes.pop("top", {}))
        for attribute_id, value in changes.pop("attributes", {}).items():
            body["attributes"] = [a for a in body["attributes"] if a["id"] != attribute_id]
            if value is not None:
                body["attributes"].append({"id": attribute_id, "value_name": value})
        return {
            "body": body,
            "changes": [
                {"field": "SELLER_PACKAGE_WEIGHT", "why": "A boxed 75 ml perfume weighs about 450 g.", "estimated": True}
            ],
        }

    def suggest(self, answer):
        fake = FakeMistral(answer)
        with patch("vimple.ai.urlopen", side_effect=fake):
            response = self.client.post(f"/catalog/meli/new/{self.refused.pk}/fix/")
        return response, fake

    def apply(self, body, **extra):
        return self.client.post(
            f"/catalog/meli/new/{self.refused.pk}/fix/apply/", {"fix_body": json.dumps(body), **extra}
        )

    def test_a_refused_draft_offers_the_ai_fix_and_nothing_changes_until_it_is_used(self):
        self.assertEqual(self.refused.status, ListingDraft.Status.NEEDS_FIX)
        page = self.client.get(f"/catalog/meli/new/{self.refused.pk}/")
        self.assertContains(page, "AI fix")
        self.assertContains(page, "Suggest a fix")
        validated = len(self.meli.validated)
        response, fake = self.suggest(self.suggestion(attributes={"SELLER_PACKAGE_WEIGHT": "450 g"}))
        self.assertContains(response, "SELLER_PACKAGE_WEIGHT")
        self.assertContains(response, "AI guess: check it")
        self.assertContains(response, "Use this and check")
        self.assertContains(response, "Also save package size and weight to the product")
        asked = json.loads(fake.asked[0]["messages"][1]["content"])
        self.assertEqual(asked["sent"]["catalog_product_id"], CATALOG)
        self.assertIn("cause", asked["response"])
        self.assertNotIn("access", json.dumps(asked))
        draft = self.draft()
        self.assertEqual((draft.overrides, draft.status), ({}, ListingDraft.Status.NEEDS_FIX))
        self.assertEqual(len(self.meli.validated), validated)
        # The suggestion stays on the page for this error.
        self.assertContains(self.client.get(f"/catalog/meli/new/{draft.pk}/"), "AI guess: check it")

    def test_using_the_fix_sends_it_but_never_the_fields_vimple_owns(self):
        body = self.suggestion(
            attributes={"SELLER_PACKAGE_WEIGHT": "450 g", "SELLER_SKU": "OTHER-SKU"},
            top={"price": 1, "available_quantity": 99, "catalog_product_id": "MLA1", "title": "Another title"},
        )["body"]
        response = self.apply(body)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["HX-Redirect"], f"/catalog/meli/new/{self.refused.pk}/")
        draft = self.draft()
        self.assertEqual(draft.status, ListingDraft.Status.READY, draft.error)
        sent = self.meli.validated[-1]
        values = {a["id"]: a.get("value_name") for a in sent["attributes"]}
        self.assertEqual(values["SELLER_PACKAGE_WEIGHT"], "450 g")
        self.assertEqual(values["SELLER_SKU"], self.product.sku)
        self.assertEqual((sent["price"], sent["available_quantity"], sent["catalog_product_id"]), (65000, 1, CATALOG))
        self.assertEqual(sent["title"], "Another title")
        self.assertNotIn("price", draft.overrides)
        self.assertContains(self.client.get(f"/catalog/meli/new/{draft.pk}/"), "Checked with an AI fix")

    def test_the_package_can_go_to_the_product(self):
        self.meli.product_package = []
        body = self.suggestion(attributes={"SELLER_PACKAGE_WEIGHT": "0.45 kg", "SELLER_PACKAGE_HEIGHT": "15 cm"})["body"]
        MasterProduct.objects.filter(pk=self.product.pk).update(height_cm=3, width_cm=10, length_cm=20, weight_g=100)
        self.apply(body, save_package="1")
        self.product.refresh_from_db()
        self.assertEqual((self.product.weight_g, self.product.height_cm), (450, Decimal("15.0")))
        self.assertEqual(self.draft().status, ListingDraft.Status.READY, self.draft().error)
        values = {a["id"]: a.get("value_name") for a in self.meli.validated[-1]["attributes"]}
        self.assertEqual((values["SELLER_PACKAGE_WEIGHT"], values["SELLER_PACKAGE_HEIGHT"]), ("450 g", "15 cm"))

    def test_unticked_the_product_keeps_its_package(self):
        before = MasterProduct.objects.get(pk=self.product.pk).weight_g
        self.apply(self.suggestion(attributes={"SELLER_PACKAGE_WEIGHT": "450 g"})["body"])
        self.assertEqual(MasterProduct.objects.get(pk=self.product.pk).weight_g, before)
        self.assertEqual(self.draft().overrides["attributes"]["SELLER_PACKAGE_WEIGHT"], {"value_name": "450 g"})

    def test_remove_fix_goes_back_to_the_plain_body(self):
        self.apply(self.suggestion(top={"title": "Another title"})["body"])
        self.client.post(f"/catalog/meli/new/{self.refused.pk}/fix/remove/")
        draft = self.draft()
        self.assertEqual(draft.overrides, {})
        self.assertNotEqual(self.meli.validated[-1].get("title"), "Another title")

    def test_the_post_sends_the_fix_then_forgets_it(self):
        self.apply(self.suggestion(top={"title": "Another title"})["body"])
        self.client.post("/catalog/meli/new/", {"action": "post", "draft": [str(self.refused.pk)]})
        draft = self.draft()
        self.assertEqual(draft.status, ListingDraft.Status.PUBLISHED, draft.error)
        self.assertEqual(self.meli.created[-1]["title"], "Another title")
        self.assertEqual((draft.overrides, draft.ai_fix), ({}, {}))

    def test_bad_json_or_a_bad_body_is_shown_and_nothing_is_kept(self):
        response = self.client.post(f"/catalog/meli/new/{self.refused.pk}/fix/apply/", {"fix_body": "{nope"})
        self.assertContains(response, "That isn&#x27;t valid JSON")
        self.assertContains(response, "{nope")
        response = self.apply({"attributes": "x"})
        self.assertContains(response, "attributes must be a list")
        self.assertEqual(self.draft().overrides, {})
        self.assertEqual(self.draft().status, ListingDraft.Status.NEEDS_FIX)

    def test_mistral_failing_or_changing_nothing_says_so(self):
        response, _ = self.suggest({"answer": "no body"})
        self.assertContains(response, "The suggestion didn&#x27;t come through")
        response, _ = self.suggest({"body": self.refused.error_details["sent"], "changes": []})
        self.assertContains(response, "The AI found nothing to change")

    def test_a_viewer_or_another_organization_cant_use_it(self):
        self.client.force_login(member(self.organization, role=Membership.Role.CONTRIBUTOR, username="bea"))
        self.assertEqual(self.client.post(f"/catalog/meli/new/{self.refused.pk}/fix/").status_code, 403)
        self.assertEqual(self.apply({}).status_code, 403)
        self.assertNotContains(self.client.get(f"/catalog/meli/new/{self.refused.pk}/"), "Suggest a fix")
        other = shared_seller("Other", user_id="999")
        self.client.force_login(member(other, username="cy"))
        self.assertEqual(self.client.post(f"/catalog/meli/new/{self.refused.pk}/fix/").status_code, 404)
        self.assertEqual(self.apply({}).status_code, 404)
        self.assertEqual(self.client.post(f"/catalog/meli/new/{self.refused.pk}/fix/remove/").status_code, 404)

    @override_settings(MISTRAL_API_KEY="")
    def test_no_key_no_ai_fix(self):
        self.assertNotContains(self.client.get(f"/catalog/meli/new/{self.refused.pk}/"), "Suggest a fix")


class OverrideTests(DraftTestCase):
    def test_a_value_off_mercado_libres_list_still_stops_before_validate(self):
        draft = ListingDraft.objects.create(
            organization=self.organization, product=self.product, price=Decimal("65000"), quantity=1,
            kind=ListingDraft.Kind.PRODUCT, category_id="MLA1271",
            category_schema=[{"id": "COLOR", "name": "Color", "required": False, "type": "list",
                              "values": ["Rojo", "Azul"], "value_ids": ["1", "2"]}],
            overrides={"attributes": {"COLOR": {"value_name": "Verde"}}},
        )
        body = build_item(draft, defaults_for(self.organization))
        self.assertIn({"id": "COLOR", "value_name": "Verde"}, body["attributes"])
        from .drafts import invalid_values

        self.assertEqual([item["id"] for item in invalid_values(draft, body)], ["COLOR"])

    def test_package_values_are_read_with_their_units(self):
        values, unread = package_from_fix(
            {"attributes": {"SELLER_PACKAGE_HEIGHT": {"value_name": "45 mm"},
                            "SELLER_PACKAGE_WEIGHT": {"value_name": "1,2 kg"},
                            "SELLER_PACKAGE_WIDTH": {"value_name": "about ten"},
                            "SELLER_PACKAGE_LENGTH": {"value_name": "300 g"}}}
        )
        self.assertEqual(values, {"height_cm": Decimal("4.5"), "weight_g": 1200})
        self.assertEqual(sorted(unread), ["SELLER_PACKAGE_LENGTH", "SELLER_PACKAGE_WIDTH"])
