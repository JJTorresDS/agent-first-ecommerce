from unittest.mock import patch
from urllib.parse import urlparse

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase

from accounts.models import Membership, Organization
from catalog.models import Combo
from catalog.test_views import member
from catalog.tests import IMAGE, product
from catalog.validators import clean_category_id

from .tests import TokenResponse, shared_seller

GUESSES = [
    {"domain_id": "MLA-PERFUMES", "domain_name": "Perfumes", "category_id": "MLA1271", "category_name": "Perfumes"},
    {"domain_id": "MLA-PERFUMES", "domain_name": "Perfumes", "category_id": "MLA1271", "category_name": "Perfumes"},
    {"domain_id": "MLA-KITS", "domain_name": "Kits de perfumería", "category_id": "MLA4321", "category_name": "Sets"},
    {"domain_id": "MLA-OTHER", "domain_name": "Otros", "category_id": "MLA9999", "category_name": "Otros"},
]


class CategorySuggestionTests(TestCase):
    def setUp(self):
        cache.clear()
        self.organization = shared_seller("Molihaus")
        self.client.force_login(member(self.organization))
        self.calls = []

    def api(self, request, timeout=15):
        self.calls.append(urlparse(request.full_url).path)
        return TokenResponse(GUESSES)

    def test_the_top_two_distinct_categories_come_back_as_codes_with_names(self):
        with patch("meli.client.urlopen", side_effect=self.api):
            response = self.client.get("/catalog/meli/categories/", {"title": "Kit perfume 885"})
        self.assertContains(response, '<option value="MLA1271">Perfumes</option>', html=False)
        self.assertContains(response, '<option value="MLA4321">Sets (Kits de perfumería)</option>', html=False)
        self.assertNotContains(response, "MLA9999")
        self.assertContains(response, '<datalist id="category-suggestions">', html=False)
        self.assertEqual(self.calls, ["/sites/MLA/domain_discovery/search"])
        # The same title again is answered from the cache.
        with patch("meli.client.urlopen", side_effect=self.api):
            self.client.get("/catalog/meli/categories/", {"title": "kit  perfume 885"})
        self.assertEqual(len(self.calls), 1)

    def test_without_a_connection_or_a_title_it_says_why(self):
        self.assertContains(self.client.get("/catalog/meli/categories/"), "Type the title")
        other = Organization.objects.create(name="Unconnected")
        self.client.force_login(member(other, username="bea"))
        with patch("meli.client.urlopen", side_effect=self.api):
            response = self.client.get("/catalog/meli/categories/", {"title": "Kit"})
        self.assertContains(response, "Connect Mercado Libre")
        self.assertEqual(self.calls, [])

    def test_contributors_can_read_suggestions(self):
        self.client.force_login(member(self.organization, role=Membership.Role.CONTRIBUTOR, username="cid"))
        with patch("meli.client.urlopen", side_effect=self.api):
            self.assertEqual(self.client.get("/catalog/meli/categories/", {"title": "Kit"}).status_code, 200)
        self.assertEqual(self.client.post("/catalog/meli/categories/", {"title": "Kit"}).status_code, 405)


class CategoryCodeTests(TestCase):
    def test_only_the_code_is_kept(self):
        self.assertEqual(clean_category_id("MLA1271 (Perfumes)"), "MLA1271")
        self.assertEqual(clean_category_id(" mla1271 — Perfumes"), "MLA1271")
        self.assertEqual(clean_category_id(""), "")
        with self.assertRaises(ValidationError):
            clean_category_id("Perfumes")

    def test_a_combo_saves_only_the_code(self):
        organization = Organization.objects.create(name="Test Seller")
        self.client.force_login(member(organization))
        first = product(organization, variant="30ML")
        second = product(organization, variant="100ML")
        data = {
            "code": "885MIX", "title": "Kit 885", "image_url": IMAGE, "status": "active", "currency": "ARS",
            "list_price": "45000", "meli_category_id": "MLA1271 (Perfumes)",
            "combo_sku": [first.sku, second.sku], "combo_quantity": ["1", "1"],
        }
        self.client.post("/catalog/combos/new/", data)
        self.assertEqual(Combo.objects.get().meli_category_id, "MLA1271")
        data.update(code="OTHER", meli_category_id="Perfumes")
        self.assertContains(self.client.post("/catalog/combos/new/", data), "Enter a Mercado Libre category code")
        self.assertEqual(Combo.objects.count(), 1)
