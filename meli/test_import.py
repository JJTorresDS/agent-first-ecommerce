from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.test import TestCase

from accounts.models import Membership
from catalog.models import Brand, Category, MasterProduct, ProductModel
from catalog.test_views import member
from catalog.tests import product_model

from .importer import ImportProblem, source_of
from .tests import TokenResponse, shared_seller

CATALOG = "MLA46233297"
PICTURES = ["https://http2.mlstatic.com/885-a.webp", "https://http2.mlstatic.com/885-b.webp"]
ATTRIBUTES = [
    {"id": "BRAND", "name": "Marca", "value_name": "Onlyou"},
    {"id": "UNIT_VOLUME", "name": "Volumen de la unidad", "value_name": "30 mL"},
    {"id": "GTIN", "name": "Código universal", "value_name": "7798123456789"},
    {"id": "PERFUME_TYPE", "name": "Tipo de perfume", "values": [{"name": "Eau de parfum"}]},
    {"id": "SELLER_SKU", "name": "SKU", "value_name": "theirs-1"},
    {"id": "SELLER_PACKAGE_WEIGHT", "name": "Peso", "value_name": "200 g"},
]


class FakeCatalog:
    def __init__(self):
        self.calls = []

    def __call__(self, request, timeout=15):
        url = urlparse(request.full_url)
        self.calls.append(url.path)
        if url.path == f"/products/{CATALOG}":
            return TokenResponse(
                {
                    "id": CATALOG,
                    "name": "Perfume Número 885, 30 Ml By Onlyou Para Hombre",
                    "status": "active",
                    "domain_id": "MLA-PERFUMES",
                    "attributes": ATTRIBUTES,
                    "pictures": [{"url": url} for url in PICTURES],
                    "short_description": {"type": "plaintext", "content": "Eau de parfum amaderada."},
                }
            )
        if url.path == "/sites/MLA/domain_discovery/search":
            return TokenResponse([{"domain_id": "MLA-PERFUMES", "category_id": "MLA1271"}])
        if url.path == "/items":
            assert parse_qs(url.query)["ids"] == ["MLA1234567890"]
            body = {
                "id": "MLA1234567890",
                "title": "Lampara Velador Led Recargable",
                "category_id": "MLA39965",
                "catalog_product_id": None,
                "attributes": [{"id": "BRAND", "value_name": "Raitot"}, {"id": "COLOR", "value_name": "Negro"}],
                "pictures": [{"secure_url": "https://http2.mlstatic.com/lamp.jpg", "url": "http://x/lamp.jpg"}],
            }
            return TokenResponse([{"code": 200, "body": body}])
        if url.path == "/items/MLA1234567890/description":
            return TokenResponse({"plain_text": "Lámpara táctil."})
        raise AssertionError(request.full_url)


class SourceTests(TestCase):
    def test_catalog_pages_listings_and_up_links_are_understood(self):
        self.assertEqual(source_of(f"https://www.mercadolibre.com.ar/perfume-885/p/{CATALOG}#x"), ("product", CATALOG))
        self.assertEqual(
            source_of("https://articulo.mercadolibre.com.ar/MLA-1234567890-lampara-_JM"), ("item", "MLA1234567890")
        )
        self.assertEqual(
            source_of("https://www.mercadolibre.com.ar/perfume/up/MLAU3874307988?pdp_filters=x#wid=MLA1724273231&sid=r"),
            ("item", "MLA1724273231"),
        )
        with self.assertRaises(ImportProblem):
            source_of("https://example.com/perfume")


class ImportTests(TestCase):
    def setUp(self):
        self.organization = shared_seller("Molihaus")
        self.client.force_login(member(self.organization))
        self.category = Category.objects.create(organization=self.organization, code="PERF", name="Perfumes")
        self.brand = Brand.objects.create(organization=self.organization, code="ONLY", name="Onlyou")
        self.meli = FakeCatalog()
        patcher = patch("meli.client.urlopen", side_effect=self.meli)
        patcher.start()
        self.addCleanup(patcher.stop)

    def read(self, url=f"https://www.mercadolibre.com.ar/p/{CATALOG}"):
        return self.client.post("/catalog/import/", {"action": "read", "url": url})

    def save(self, **fields):
        data = {
            "action": "save",
            "url": f"https://www.mercadolibre.com.ar/p/{CATALOG}",
            "meli_catalog_product_id": CATALOG,
            "meli_category_id": "MLA1271",
            "category": str(self.category.pk),
            "brand": str(self.brand.pk),
            "model_code": "885",
            "model_name": "Número 885",
            "variant": "30ml",
            "title": "Perfume Número 885 30 ml",
            "gtin": "7798123456789",
            "description": "Mi descripción.",
            "picture_option": PICTURES,
            "picture": [PICTURES[1]],
            "attr_key": ["PERFUME_TYPE"],
            "attr_value": ["Eau de parfum"],
        }
        data.update(fields)
        return self.client.post("/catalog/import/", data)

    def test_a_catalog_page_is_read_into_a_form_to_review(self):
        page = self.read()
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'value="Perfume Número 885, 30 Ml By Onlyou Para Hombre"')
        self.assertContains(page, f'<option value="{self.brand.pk}" selected>')
        self.assertContains(page, 'name="variant" value="30ML"')
        self.assertContains(page, 'name="gtin" value="7798123456789"')
        self.assertContains(page, 'value="PERFUME_TYPE"')
        self.assertContains(page, 'value="Eau de parfum"')
        self.assertNotContains(page, "SELLER_SKU")
        self.assertNotContains(page, "SELLER_PACKAGE_WEIGHT")
        self.assertContains(page, f'name="meli_category_id" value="MLA1271"')
        self.assertContains(page, "Eau de parfum amaderada.")
        for url in PICTURES:
            self.assertContains(page, f'name="picture" value="{url}" checked')
        # Nothing saved by reading.
        self.assertFalse(MasterProduct.objects.exists())
        # No Mistral key in tests: no AI buttons.
        self.assertNotContains(page, "Suggest codes")

    def test_a_listing_link_is_read_with_its_description(self):
        page = self.read("https://articulo.mercadolibre.com.ar/MLA-1234567890-lampara-_JM")
        self.assertContains(page, 'value="Lampara Velador Led Recargable"')
        self.assertContains(page, "Lámpara táctil.")
        self.assertContains(page, 'value="https://http2.mlstatic.com/lamp.jpg"')
        self.assertContains(page, 'name="meli_category_id" value="MLA39965"')

    def test_saving_makes_the_model_product_and_pictures(self):
        response = self.save()
        product = MasterProduct.objects.get(organization=self.organization)
        self.assertRedirects(response, f"/catalog/products/{product.pk}/")
        self.assertEqual(product.sku, "PERF-ONLY-885-30ML")
        self.assertEqual(product.image_url, PICTURES[1])
        self.assertEqual(list(product.images.values_list("url", flat=True)), [PICTURES[1]])
        self.assertEqual((product.meli_catalog_product_id, product.meli_category_id), (CATALOG, "MLA1271"))
        self.assertEqual(product.attributes, {"PERFUME_TYPE": "Eau de parfum"})
        self.assertEqual(product.product_model.description, "Mi descripción.")

    def test_an_existing_model_of_the_same_brand_takes_the_variant(self):
        model = product_model(self.organization, code="885", brand="ONLY", category="PERF")
        self.save(product_model=str(model.pk), model_code="", model_name="")
        self.assertEqual(MasterProduct.objects.get().product_model, model)
        other = product_model(self.organization, code="900", brand="LATT", category="PERF")
        response = self.save(product_model=str(other.pk), variant="50ML", gtin="")
        self.assertContains(response, "That model has another category or brand.")

    def test_problems_are_shown_and_nothing_is_saved(self):
        self.assertContains(self.save(picture=[]), "Pick at least one picture.")
        self.assertContains(self.save(model_code=""), "Pick an existing model or give the new one&#x27;s code and name.")
        self.assertContains(self.save(variant="30 ml!"), "Variant")
        self.assertFalse(MasterProduct.objects.exists())
        self.assertFalse(ProductModel.objects.exists())

    def test_another_organizations_category_or_brand_cant_be_used(self):
        other = shared_seller("Other", user_id="777")
        theirs = Category.objects.create(organization=other, code="PERF", name="Perfumes")
        response = self.save(category=str(theirs.pk))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(MasterProduct.objects.exists())

    def test_only_editors_import(self):
        self.client.force_login(member(self.organization, role=Membership.Role.CONTRIBUTOR, username="bea"))
        self.assertContains(self.client.get("/catalog/import/"), "Only owners and admins can import products.")
        self.assertEqual(self.read().status_code, 403)
        self.assertEqual(self.meli.calls, [])
        self.client.force_login(member(self.organization, apps=(), username="cid"))
        self.assertEqual(self.client.get("/catalog/import/").status_code, 403)
