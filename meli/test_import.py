from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.test import TestCase, override_settings

from accounts.models import Membership, Organization
from catalog.models import Brand, Category, MasterProduct, ProductModel
from catalog.test_views import member
from catalog.tests import product_model
from vimple import ai

from .client import MercadoLibreError
from .importer import ImportProblem, source_of
from catalog.models import RootCategory

from .models import MeliListingProspect
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
        if url.path == "/categories/MLA1271/attributes":
            return TokenResponse(CATEGORY_ATTRIBUTES)
        if url.path == "/categories/MLA1271":
            return TokenResponse(
                {"id": "MLA1271", "name": "Perfumes",
                 "path_from_root": [{"id": "MLA1246", "name": "Belleza y Cuidado Personal"}, {"id": "MLA1271", "name": "Perfumes"}]}
            )
        raise AssertionError(request.full_url)


CATEGORY_ATTRIBUTES = [
    {"id": "PERFUME_TYPE", "name": "Tipo de perfume", "value_type": "list", "tags": {},
     "values": [{"id": "1", "name": "Eau de parfum"}, {"id": "2", "name": "Eau de toilette"}]},
    {"id": "GENDER", "name": "Género", "value_type": "list", "tags": {},
     "values": [{"id": "3", "name": "Hombre"}, {"id": "4", "name": "Mujer"}]},
    {"id": "SELLER_PACKAGE_WEIGHT", "name": "Peso", "value_type": "number_unit", "tags": {}},
]
SCRAPED = {
    "productId": "MLAU3874307988",
    "title": "Perfume Onlyou 885 30ml Envío Gratis",
    "pageUrl": "https://www.mercadolibre.com.ar/perfume-885/up/MLAU3874307988",
    "currentPrice": 12999,
    "seller": "OTRATIENDA",
    "images": {"gallery": ["https://http2.mlstatic.com/scraped-1.webp", "https://http2.mlstatic.com/scraped-2.webp"]},
    "characteristics": {"Marca": "Onlyou", "Volumen": "30 mL", "Género": "Hombre"},
}


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
            "list_price": "15999",
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
        self.assertContains(self.save(list_price=""), "This field is required.")
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


MAPPED = {
    "category_code": "PERF",
    "category_name": "Perfumes",
    "brand_code": "ONLY",
    "brand_name": "Onlyou",
    "model_code": "885",
    "model_name": "Número 885",
    "variant": "30ml",
    "title": "Perfume Onlyou Número 885 30 ml",
    "gtin": "7798123456789",
    "attributes": {"GENDER": "hombre", "PERFUME_TYPE": "Eau de toilette", "COLOR": "Rojo"},
}


def prospect(organization, external_id="MLAU3874307988", **fields):
    payload = {**SCRAPED, "productId": external_id, **fields.pop("payload", {})}
    return MeliListingProspect.objects.create(
        organization=organization,
        external_id=external_id,
        title=payload["title"],
        url=payload["pageUrl"],
        price=payload["currentPrice"],
        picture=payload["images"]["gallery"][0],
        seller=payload["seller"],
        category_id="MLA1271",
        category_path="Belleza y Cuidado Personal > Perfumes",
        payload=payload,
        **fields,
    )


class ScrapedImportTests(TestCase):
    def setUp(self):
        self.organization = shared_seller("Molihaus")
        self.client.force_login(member(self.organization))
        self.category = Category.objects.create(organization=self.organization, code="PERF", name="Perfumes")
        self.brand = Brand.objects.create(organization=self.organization, code="ONLY", name="Onlyou")
        self.meli = FakeCatalog()
        patcher = patch("meli.client.urlopen", side_effect=self.meli)
        patcher.start()
        self.addCleanup(patcher.stop)

    def save(self, scraped, **fields):
        data = {
            "action": "save",
            "prospect": str(scraped.pk),
            "category": str(self.category.pk),
            "brand": str(self.brand.pk),
            "model_code": "885",
            "model_name": "Número 885",
            "variant": "30ML",
            "title": "Perfume Número 885 30 ml",
            "list_price": "12999",
            "picture_option": PICTURES,
            "picture": PICTURES,
        }
        data.update(fields)
        return self.client.post("/catalog/import/", data)

    def test_the_tab_lists_only_this_organizations_unlinked_scraped_listings(self):
        waiting = prospect(self.organization)
        prospect(self.organization, external_id="MLA111111111", status=MeliListingProspect.Status.DISMISSED,
                 payload={"title": "Dismissed one"})
        linked = prospect(self.organization, external_id="MLA222222222", payload={"title": "Linked one"})
        model = product_model(self.organization, code="885", brand="ONLY", category="PERF")
        linked.product = MasterProduct.objects.create(
            organization=self.organization, product_model=model, variant="30ML", title="Mine", list_price=1
        )
        linked.save()
        other = Organization.objects.create(name="Other")
        prospect(other, external_id="MLA333333333", payload={"title": "Their scraped one"})

        page = self.client.get("/catalog/import/?start=scraped")
        self.assertContains(page, f'href="?prospect={waiting.pk}"')
        self.assertContains(page, "Perfume Onlyou 885 30ml Envío Gratis")
        for title in ("Dismissed one", "Linked one", "Their scraped one"):
            self.assertNotContains(page, title)
        # The link tab is the default and lists nothing scraped.
        self.assertNotContains(self.client.get("/catalog/import/"), "Create product")
        self.assertEqual(self.meli.calls, [])

    def test_another_organizations_or_a_linked_scraped_listing_is_404(self):
        theirs = prospect(Organization.objects.create(name="Other"))
        self.assertEqual(self.client.get(f"/catalog/import/?prospect={theirs.pk}").status_code, 404)
        self.assertEqual(self.client.get("/catalog/import/?prospect=nope").status_code, 404)

    def test_a_catalog_page_link_is_read_through_the_api(self):
        scraped = prospect(self.organization, payload={"pageUrl": f"https://www.mercadolibre.com.ar/x/p/{CATALOG}"})
        page = self.client.get(f"/catalog/import/?prospect={scraped.pk}")
        self.assertContains(page, 'value="Perfume Número 885, 30 Ml By Onlyou Para Hombre"')
        self.assertContains(page, f'name="prospect" value="{scraped.pk}"')
        self.assertContains(page, 'name="list_price" value="12999')
        self.assertContains(page, "From scraped listing")
        self.assertContains(page, f'name="meli_catalog_product_id" value="{CATALOG}"')
        self.assertNotContains(page, "come from the scraped page")

    def test_without_a_readable_link_the_scrape_fills_the_form(self):
        scraped = prospect(self.organization)
        page = self.client.get(f"/catalog/import/?prospect={scraped.pk}")
        self.assertContains(page, "come from the scraped page")
        self.assertContains(page, 'value="Perfume Onlyou 885 30ml Envío Gratis"')
        self.assertContains(page, f'<option value="{self.brand.pk}" selected>')
        self.assertContains(page, 'name="variant" value="30ML"')
        self.assertContains(page, 'name="picture" value="https://http2.mlstatic.com/scraped-2.webp" checked')
        self.assertContains(page, 'value="Género"')
        self.assertContains(
            page, 'Mercado Libre\'s category: <span class="mono">MLA1271</span> · <strong>Belleza y Cuidado Personal &gt; Perfumes</strong>'
        )
        self.assertEqual(self.meli.calls, [])

    def test_a_refused_read_falls_back_to_the_scrape(self):
        scraped = prospect(self.organization, payload={"pageUrl": f"https://www.mercadolibre.com.ar/x/p/{CATALOG}"})
        with patch("meli.importer.read", side_effect=MercadoLibreError("Resource not found", status=404)):
            page = self.client.get(f"/catalog/import/?prospect={scraped.pk}")
        self.assertContains(page, "Mercado Libre couldn&#x27;t read it (Resource not found)")
        self.assertContains(page, 'value="Perfume Onlyou 885 30ml Envío Gratis"')

    @override_settings(MISTRAL_API_KEY="test-key")
    def test_the_ai_maps_the_scrape_onto_the_form_after_a_retry(self):
        scraped = prospect(self.organization, payload={"pageUrl": f"https://www.mercadolibre.com.ar/x/p/{CATALOG}"})
        with patch("vimple.ai._complete_json", side_effect=[ai.SuggestionFailed("URLError"), MAPPED]) as call:
            page = self.client.get(f"/catalog/import/?prospect={scraped.pk}")
        self.assertEqual(call.call_count, 2)
        sent = call.call_args.args[1]
        self.assertEqual(sent["scraped_listing"]["characteristics"]["Marca"], "Onlyou")
        self.assertEqual([item["id"] for item in sent["attributes"]], ["GENDER"])
        self.assertContains(page, f'<option value="{self.category.pk}" selected>')
        self.assertContains(page, f'<option value="{self.brand.pk}" selected>')
        self.assertContains(page, 'name="model_code" value="885"')
        self.assertContains(page, 'name="model_name" value="Número 885"')
        # The API's own values win over the AI's.
        self.assertContains(page, 'value="Perfume Número 885, 30 Ml By Onlyou Para Hombre"')
        self.assertContains(page, 'value="GENDER"')
        self.assertContains(page, 'value="Hombre"')
        self.assertNotContains(page, 'value="Eau de toilette"')
        self.assertNotContains(page, 'value="COLOR"')
        self.assertContains(page, "the AI&#x27;s suggestion")

    @override_settings(MISTRAL_API_KEY="test-key")
    def test_ai_codes_new_to_the_organization_wait_in_their_dialog_or_pick_an_existing_model(self):
        model = product_model(self.organization, code="885", brand="ONLY", category="PERF")
        scraped = prospect(self.organization)
        answer = {**MAPPED, "category_code": "FRAG", "category_name": "Fragancias", "root_code": "beauty",
                  "root_name": "Belleza"}
        with patch("vimple.ai._complete_json", return_value=answer) as call:
            page = self.client.get(f"/catalog/import/?prospect={scraped.pk}")
        self.assertEqual(call.call_args.args[1]["meli_category_path"], "Belleza y Cuidado Personal > Perfumes")
        self.assertContains(page, "Suggested new category FRAG · Fragancias: add it with +.")
        self.assertContains(page, 'name="category-code" value="FRAG"')
        self.assertContains(page, 'name="category-root_code" value="BEAUTY"')
        self.assertContains(page, 'name="category-root_name" value="Belleza"')
        self.assertContains(page, f'<option value="{model.pk}" selected>')
        # From the scrape, the AI's clean title and its attributes replace the raw ones.
        self.assertContains(page, 'value="Perfume Onlyou Número 885 30 ml"')
        self.assertContains(page, 'value="GENDER"')
        self.assertNotContains(page, 'value="Género"')

    @override_settings(MISTRAL_API_KEY="test-key")
    def test_a_failed_ai_call_still_shows_the_form(self):
        scraped = prospect(self.organization)
        with patch("vimple.ai._complete_json", side_effect=ai.SuggestionFailed("HTTP 401")) as call:
            page = self.client.get(f"/catalog/import/?prospect={scraped.pk}")
        self.assertEqual(call.call_count, 1)
        self.assertContains(page, "The AI couldn&#x27;t map the scraped listing")
        self.assertContains(page, 'value="Perfume Onlyou 885 30ml Envío Gratis"')

    def test_the_root_category_is_offered_from_mercado_libres_path(self):
        scraped = prospect(self.organization)
        page = self.client.get(f"/catalog/import/?prospect={scraped.pk}")
        self.assertContains(page, "Mercado Libre's root category: <strong>Belleza y Cuidado Personal</strong>")
        self.assertContains(page, 'name="category-root_name" value="Belleza y Cuidado Personal"')
        RootCategory.objects.create(organization=Organization.objects.create(name="Other"), code="THEIRS",
                                    name="Belleza y cuidado personal")
        mine = RootCategory.objects.create(organization=self.organization, code="BEAUTY", name="belleza y cuidado personal")
        page = self.client.get(f"/catalog/import/?prospect={scraped.pk}")
        self.assertContains(page, f'<option value="{mine.pk}" selected>BEAUTY · belleza y cuidado personal</option>')
        self.assertNotContains(page, "THEIRS")
        self.assertContains(page, 'name="category-root_name" value=""')

    def test_a_pasted_links_category_path_is_read_once(self):
        page = self.client.post("/catalog/import/", {"action": "read", "url": f"https://www.mercadolibre.com.ar/p/{CATALOG}"})
        self.assertContains(page, "<strong>Belleza y Cuidado Personal &gt; Perfumes</strong>")
        self.assertContains(page, 'name="meli_category_path" value="Belleza y Cuidado Personal &gt; Perfumes"')
        self.assertContains(page, 'name="category-root_name" value="Belleza y Cuidado Personal"')

    def test_saving_links_the_scraped_listing_to_the_new_product(self):
        scraped = prospect(self.organization)
        response = self.save(scraped)
        product = MasterProduct.objects.get(organization=self.organization)
        self.assertRedirects(response, f"/catalog/products/{product.pk}/")
        self.assertEqual(product.sku, "PERF-ONLY-885-30ML")
        scraped.refresh_from_db()
        self.assertEqual(scraped.product, product)
        self.assertNotContains(self.client.get("/catalog/import/?start=scraped"), f"?prospect={scraped.pk}")

    def test_an_existing_model_takes_the_new_variant(self):
        model = product_model(self.organization, code="885", brand="ONLY", category="PERF")
        scraped = prospect(self.organization)
        self.save(scraped, product_model=str(model.pk), model_code="", model_name="", variant="50ML")
        scraped.refresh_from_db()
        self.assertEqual(scraped.product.product_model, model)
        self.assertEqual(scraped.product.variant, "50ML")

    def test_a_form_error_keeps_the_scraped_listing(self):
        scraped = prospect(self.organization)
        page = self.save(scraped, list_price="")
        self.assertContains(page, "This field is required.")
        self.assertContains(page, f'name="prospect" value="{scraped.pk}"')
        self.assertContains(page, "From scraped listing")

    def test_another_organizations_scraped_listing_is_never_linked(self):
        theirs = prospect(Organization.objects.create(name="Other"))
        self.save(theirs)
        product = MasterProduct.objects.get()
        self.assertEqual(product.organization, self.organization)
        theirs.refresh_from_db()
        self.assertIsNone(theirs.product)

    def test_only_editors_create_from_a_scraped_listing(self):
        scraped = prospect(self.organization)
        self.client.force_login(member(self.organization, role=Membership.Role.CONTRIBUTOR, username="bea"))
        self.assertEqual(self.save(scraped).status_code, 403)
        self.assertContains(self.client.get(f"/catalog/import/?prospect={scraped.pk}"), "Only owners and admins")
        self.assertFalse(MasterProduct.objects.exists())
        scraped.refresh_from_db()
        self.assertIsNone(scraped.product)
