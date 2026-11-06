import json
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from accounts.models import Membership, Organization

from .images import set_images
from .models import Brand, Category, ProductImage
from .test_views import member
from .tests import product


class FakeMistral:
    """Answers like Mistral's chat completions, and keeps what it was asked."""

    def __init__(self, answer):
        self.answer = answer
        self.asked = []

    def __call__(self, request, timeout=25):
        self.asked.append(json.loads(request.data.decode()))
        payload = {"choices": [{"message": {"content": json.dumps(self.answer)}}]}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(payload).encode()

        return Response()


class PictureTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Seller")
        self.item = product(self.organization, variant="30ML")

    def test_the_first_picture_is_the_main_one_and_order_is_kept(self):
        urls = ["https://img.example/a.jpg", "https://img.example/b.jpg", "https://img.example/a.jpg", ""]
        set_images(self.item, urls)
        self.item.refresh_from_db()
        self.assertEqual(self.item.image_url, "https://img.example/a.jpg")
        self.assertEqual(list(self.item.images.values_list("url", "position")), [("https://img.example/a.jpg", 0), ("https://img.example/b.jpg", 1)])
        self.assertEqual(self.item.images.first().organization, self.organization)
        set_images(self.item, ["https://img.example/c.jpg"])
        self.item.refresh_from_db()
        self.assertEqual((self.item.image_url, self.item.images.count()), ("https://img.example/c.jpg", 1))

    def test_more_than_ten_or_a_bad_url_is_refused(self):
        with self.assertRaises(ValidationError):
            set_images(self.item, [f"https://img.example/{n}.jpg" for n in range(11)])
        with self.assertRaises(ValidationError):
            set_images(self.item, ["ftp://img/a.jpg"])
        self.assertFalse(ProductImage.objects.exists())

    def test_a_picture_cant_belong_to_another_organizations_product(self):
        other = Organization.objects.create(name="Other")
        with self.assertRaises(ValidationError):
            ProductImage(organization=other, product=self.item, url="https://img.example/a.jpg").save()

    def test_the_product_page_edits_pictures_for_editors_only(self):
        self.client.force_login(member(self.organization))
        response = self.client.post(
            f"/catalog/products/{self.item.pk}/",
            {"action": "pictures", "pictures": "https://img.example/a.jpg\nhttps://img.example/b.jpg"},
        )
        self.assertRedirects(response, f"/catalog/products/{self.item.pk}/?saved=1")
        self.item.refresh_from_db()
        self.assertEqual(self.item.image_url, "https://img.example/a.jpg")
        self.assertContains(self.client.get(f"/catalog/products/{self.item.pk}/"), "https://img.example/b.jpg")
        self.client.force_login(member(self.organization, role=Membership.Role.CONTRIBUTOR, username="bea"))
        response = self.client.post(f"/catalog/products/{self.item.pk}/", {"action": "pictures", "pictures": "https://img.example/x.jpg"})
        self.assertEqual(response.status_code, 403)


@override_settings(MISTRAL_API_KEY="test-key")
class SuggestionTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Seller")
        self.client.force_login(member(self.organization))
        self.brand = Brand.objects.create(organization=self.organization, code="ONLY", name="Onlyou")

    def ask(self, kind, answer, **fields):
        fake = FakeMistral(answer)
        data = {"title": "Perfume Número 885, 30 Ml By Onlyou", "brand": str(self.brand.pk), "attr_key": ["UNIT_VOLUME"], "attr_value": ["30 mL"]}
        data.update(fields)
        with patch("vimple.ai.urlopen", side_effect=fake):
            response = self.client.post(f"/catalog/ai/{kind}/", data)
        return response, fake

    def suggested(self, response):
        self.assertEqual(response.status_code, 204)
        return json.loads(response["HX-Trigger"])["ai-suggested"]

    def test_codes_are_validated_and_existing_ones_selected(self):
        response, fake = self.ask(
            "codes",
            {
                "category_code": "perf", "category_name": "Perfumes",
                "brand_code": "ONLY", "brand_name": "Onlyou",
                "model_code": "885", "model_name": "Número 885",
                "variant": "30 ML!",
            },
        )
        suggested = self.suggested(response)
        values = suggested["values"]
        self.assertEqual(values["brand"], str(self.brand.pk))
        self.assertEqual((values["category_code"], values["model_code"]), ("PERF", "885"))
        self.assertNotIn("variant", values)
        self.assertNotIn("category", values)
        self.assertIn("new category PERF · Perfumes", suggested["message"])
        sent = fake.asked[0]
        self.assertEqual(sent["response_format"], {"type": "json_object"})
        user = json.loads(sent["messages"][1]["content"])
        self.assertEqual(user["existing_brands"], [{"code": "ONLY", "name": "Onlyou"}])
        self.assertEqual(user["product"]["brand"], "Onlyou")
        self.assertEqual(user["product"]["attributes"], {"UNIT_VOLUME": "30 mL"})

    def test_a_description_is_returned_to_review(self):
        response, _ = self.ask("description", {"description": "Una fragancia amaderada de 30 ml."})
        self.assertEqual(self.suggested(response)["values"], {"description": "Una fragancia amaderada de 30 ml."})

    def test_a_failed_call_says_so_and_fills_nothing(self):
        def broken(request, timeout=25):
            raise OSError("down")

        with patch("vimple.ai.urlopen", side_effect=broken):
            response = self.client.post("/catalog/ai/codes/", {"title": "Perfume"})
        suggested = self.suggested(response)
        self.assertEqual(suggested["values"], {})
        self.assertIn("didn't come through", suggested["message"])

    def test_another_organizations_brand_isnt_read_and_contributors_cant_ask(self):
        other = Organization.objects.create(name="Other")
        theirs = Brand.objects.create(organization=other, code="SECRET", name="Secret brand")
        _, fake = self.ask("codes", {}, brand=str(theirs.pk))
        user = json.loads(fake.asked[0]["messages"][1]["content"])
        self.assertEqual(user["product"]["brand"], "")
        self.assertNotIn("SECRET", json.dumps(user))
        self.client.force_login(member(self.organization, role=Membership.Role.CONTRIBUTOR, username="bea"))
        self.assertEqual(self.client.post("/catalog/ai/codes/", {"title": "x"}).status_code, 403)
        self.assertEqual(self.client.post("/catalog/ai/other/", {"title": "x"}).status_code, 404)

    @override_settings(MISTRAL_API_KEY="")
    def test_without_a_key_nothing_is_sent(self):
        response, fake = self.ask("codes", {"model_code": "885"})
        self.assertIn("didn't come through", self.suggested(response)["message"])
        self.assertEqual(fake.asked, [])

    def test_the_model_page_has_no_ai_buttons(self):
        self.assertNotContains(self.client.get("/catalog/models/new/"), "(AI)")


@override_settings(MISTRAL_API_KEY="test-key")
class ComboSuggestionTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Seller")
        self.client.force_login(member(self.organization))
        self.first = product(self.organization, variant="30ML", list_price="15999.00", attributes={"UNIT_VOLUME": "30 mL"})
        self.second = product(self.organization, variant="100ML", list_price="30000.00")
        model = self.first.product_model
        model.description = "Eau de parfum amaderada."
        model.save()

    def test_the_combo_page_carries_each_products_list_price_for_the_suggestion(self):
        page = self.client.get("/catalog/combos/new/")
        self.assertContains(page, f'<option value="{self.first.sku}" data-price="15999.00">', html=False)
        self.assertContains(page, "list prices minus 15%")
        self.assertContains(page, "Write with AI")

    def test_the_wand_writes_from_the_products_own_descriptions(self):
        fake = FakeMistral({"description": "Kit con dos perfumes."})
        with patch("vimple.ai.urlopen", side_effect=fake):
            response = self.client.post(
                "/catalog/ai/combo_description/",
                {"title": "Kit 885 x2", "combo_sku": [self.first.sku, self.second.sku], "combo_quantity": ["2", "1"]},
            )
        self.assertEqual(json.loads(response["HX-Trigger"])["ai-suggested"]["values"], {"description": "Kit con dos perfumes."})
        sent = json.loads(fake.asked[0]["messages"][1]["content"])
        self.assertEqual(sent["kit_title"], "Kit 885 x2")
        first = sent["products"][0]
        self.assertEqual((first["quantity"], first["description"], first["attributes"]), (2, "Eau de parfum amaderada.", {"UNIT_VOLUME": "30 mL"}))
        self.assertEqual(sent["products"][1]["quantity"], 1)

    def test_no_products_no_call_and_another_organizations_products_stay_out(self):
        fake = FakeMistral({"description": "x"})
        with patch("vimple.ai.urlopen", side_effect=fake):
            response = self.client.post("/catalog/ai/combo_description/", {"title": "Kit"})
        self.assertEqual(json.loads(response["HX-Trigger"])["ai-suggested"]["message"], "Add the products first.")
        self.assertEqual(fake.asked, [])
        other = Organization.objects.create(name="Other")
        theirs = product(other, variant="50ML", code="900")
        model = theirs.product_model
        model.description = "SECRETO"
        model.save()
        with patch("vimple.ai.urlopen", side_effect=fake):
            self.client.post("/catalog/ai/combo_description/", {"title": "Kit", "combo_sku": [theirs.sku, self.first.sku], "combo_quantity": ["1", "1"]})
        self.assertNotIn("SECRETO", json.dumps(fake.asked))

    def test_contributors_cant_ask(self):
        self.client.force_login(member(self.organization, role=Membership.Role.CONTRIBUTOR, username="bea"))
        self.assertEqual(self.client.post("/catalog/ai/combo_description/", {}).status_code, 403)

    def test_the_sellers_extra_words_reach_the_prompt_trimmed_and_only_when_given(self):
        fake = FakeMistral({"description": "Regalo para mamá."})
        lines = {"title": "Kit", "combo_sku": [self.first.sku], "combo_quantity": ["2"]}
        with patch("vimple.ai.urlopen", side_effect=fake):
            self.client.post("/catalog/ai/combo_description/", {**lines, "ai_extra": "  Día de la Madre  " + "x" * 600})
            self.client.post("/catalog/ai/combo_description/", lines)
            self.client.post("/catalog/ai/description/", {"title": "Perfume 885", "ai_extra": "Navidad"})
        first, second, single = (json.loads(asked["messages"][1]["content"]) for asked in fake.asked)
        self.assertTrue(first["seller_notes"].startswith("Día de la Madre x"))
        self.assertEqual(len(first["seller_notes"]), 500)
        self.assertNotIn("seller_notes", second)
        self.assertEqual(single["seller_notes"], "Navidad")
