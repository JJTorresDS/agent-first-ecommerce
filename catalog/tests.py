from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from accounts.models import Organization

from .models import Brand, Category, MasterProduct, ProductModel
from .validators import validate_attributes

IMAGE = "https://cdn.example.com/only-885.jpg"


def product_model(organization, code="885", brand="ONLY", category="PERF", name="Only 885 Eau de Parfum"):
    category_row, _ = Category.objects.get_or_create(
        organization=organization, code=category, defaults={"name": f"Category {category}"}
    )
    brand_row, _ = Brand.objects.get_or_create(
        organization=organization, code=brand, defaults={"name": f"Brand {brand}"}
    )
    model, _ = ProductModel.objects.get_or_create(
        organization=organization,
        brand=brand_row,
        code=code,
        defaults={"category": category_row, "name": name},
    )
    return model


def product(organization, variant="100ML", code="885", **fields):
    model = product_model(organization, code=code)
    return MasterProduct.objects.create(
        product_model=model,
        variant=variant,
        image_url=fields.pop("image_url", IMAGE),
        **fields,
    )


class SkuTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Seller")
        self.other = Organization.objects.create(name="Other")

    def test_the_sku_is_built_from_the_codes(self):
        perfumes = Category.objects.create(organization=self.organization, code=" perf ", name=" Perfumes ")
        only = Brand.objects.create(organization=self.organization, code="only", name="Only")
        model = ProductModel.objects.create(
            organization=self.organization, category=perfumes, brand=only, code="885", name="Only 885 Eau de Parfum"
        )
        small = MasterProduct.objects.create(product_model=model, variant=" 30ml ", image_url=IMAGE)
        self.assertEqual((perfumes.code, perfumes.name), ("PERF", "Perfumes"))
        self.assertEqual(model.family_code(), "PERF-ONLY-885")
        self.assertEqual(small.sku, "PERF-ONLY-885-30ML")
        self.assertEqual(small.title, "Only 885 Eau de Parfum 30ML")
        self.assertEqual(small.organization, self.organization)
        named = MasterProduct.objects.create(
            product_model=model, variant="100ML", title="Only 885 EDP 100 ml", image_url=IMAGE
        )
        self.assertEqual(named.title, "Only 885 EDP 100 ml")

    def test_bad_codes_are_refused(self):
        for bad in ("PE-RF", "PÉRF", "PE RF", "PERFUMES", "P"):
            with self.assertRaises(ValidationError):
                Category(organization=self.organization, code=bad, name=f"Bad {bad}").full_clean()
        model = product_model(self.organization)
        for bad in ("30-ML", "30 ML", "ÚNICO", "ABCDEFGHIJK", ""):
            with self.assertRaises(ValidationError):
                MasterProduct(product_model=model, variant=bad, image_url=IMAGE).full_clean()

    def test_category_and_brand_codes_and_names_are_unique_per_organization(self):
        for Kind, code in ((Category, "PERF"), (Brand, "ONLY")):
            Kind.objects.create(organization=self.organization, code=code, name="Same")
            Kind.objects.create(organization=self.other, code=code, name="Same")
            with self.assertRaises(ValidationError):
                Kind(organization=self.organization, code=code, name="Different").full_clean()
            with self.assertRaises(ValidationError):
                Kind(organization=self.organization, code="OTHER", name="same").full_clean()

    def test_a_model_code_is_unique_within_its_brand(self):
        product_model(self.organization, code="885", brand="ONLY")
        product_model(self.organization, code="885", brand="CK")
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProductModel.objects.create(
                organization=self.organization,
                category=Category.objects.get(organization=self.organization, code="PERF"),
                brand=Brand.objects.get(organization=self.organization, code="ONLY"),
                code="885",
                name="Copy",
            )

    def test_a_variant_is_unique_within_its_model(self):
        product(self.organization, variant="30ML")
        with self.assertRaises(IntegrityError), transaction.atomic():
            product(self.organization, variant="30ML")

    def test_the_sku_never_changes(self):
        item = product(self.organization, variant="30ML")
        brand = item.product_model.brand
        brand.name = "Only Fragrances"
        brand.save()
        item.refresh_from_db()
        self.assertEqual(item.sku, "PERF-ONLY-885-30ML")
        brand.code = "ONLYF"
        with self.assertRaises(ValidationError):
            brand.save()
        category = item.product_model.category
        category.code = "FRAG"
        with self.assertRaises(ValidationError):
            category.save()
        model = item.product_model
        model.code = "886"
        with self.assertRaises(ValidationError):
            model.save()
        item.variant = "50ML"
        with self.assertRaises(ValidationError):
            item.save()
        item.refresh_from_db()
        self.assertEqual(item.sku, "PERF-ONLY-885-30ML")

    def test_an_unused_code_can_still_be_fixed(self):
        brand = Brand.objects.create(organization=self.organization, code="ONLI", name="Only")
        brand.code = "ONLY"
        brand.save()
        self.assertEqual(Brand.objects.get(pk=brand.pk).code, "ONLY")

    def test_links_stay_inside_one_organization(self):
        theirs = product_model(self.other)
        mine = Category.objects.create(organization=self.organization, code="PERF", name="Perfumes")
        with self.assertRaises(ValidationError):
            ProductModel.objects.create(
                organization=self.organization, category=mine, brand=theirs.brand, code="1", name="X"
            )
        with self.assertRaises(ValidationError):
            MasterProduct.objects.create(
                organization=self.organization, product_model=theirs, variant="30ML", image_url=IMAGE
            )
        self.assertFalse(MasterProduct.objects.filter(organization=self.organization).exists())


class MasterProductTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Seller")
        self.other = Organization.objects.create(name="Other")

    def test_a_product_saves_with_its_fields_and_attributes(self):
        item = product(
            self.organization,
            gtin="7791234567890",
            attributes={"VOLUME_ML": 100, "FRAGRANCE_NOTES": ["vainilla", "caramelo"], "GIFT": True},
        )
        item.full_clean()
        item.refresh_from_db()
        self.assertEqual(item.status, MasterProduct.Status.DRAFT)
        self.assertEqual(item.product_model.condition, MasterProduct.Condition.NEW)
        self.assertEqual(item.currency, "ARS")
        self.assertEqual(item.attributes["FRAGRANCE_NOTES"], ["vainilla", "caramelo"])

    def test_the_image_url_is_required_and_must_be_http(self):
        model = product_model(self.organization)
        for bad in ("", "ftp://cdn.example.com/a.jpg", "not a url"):
            item = MasterProduct(product_model=model, variant="30ML", image_url=bad)
            with self.assertRaises(ValidationError) as caught:
                item.full_clean()
            self.assertIn("image_url", caught.exception.message_dict)

    def test_the_same_sku_can_exist_in_another_organization(self):
        mine = product(self.organization)
        theirs = product(self.other)
        self.assertEqual(mine.sku, theirs.sku)
        self.assertNotEqual(mine.organization_id, theirs.organization_id)

    def test_gtin_must_be_a_barcode_and_is_unique_when_set(self):
        model = product_model(self.organization)
        for bad in ("77912345678AB", "1234567", "123456789"):
            with self.assertRaises(ValidationError):
                MasterProduct(product_model=model, variant="X", image_url=IMAGE, gtin=bad).full_clean()
        product(self.organization, variant="A")
        product(self.organization, variant="B")
        product(self.organization, variant="C", gtin="7791234567890")
        product(self.other, variant="C", gtin="7791234567890")
        with self.assertRaises(IntegrityError), transaction.atomic():
            product(self.organization, variant="D", gtin="7791234567890")

    def test_attributes_must_be_a_flat_object(self):
        validate_attributes({"COLOR": "Rojo", "VOLUME_ML": 100, "TAGS": ["a", 1], "EMPTY": None})
        for bad in (
            ["COLOR", "Rojo"],
            {"PACKAGE": {"WEIGHT": 1}},
            {"TAGS": [{"nested": True}]},
            {"": "x"},
            {"X" * 61: "x"},
            {f"K{index}": index for index in range(101)},
        ):
            with self.assertRaises(ValidationError):
                validate_attributes(bad)
        item = MasterProduct(
            product_model=product_model(self.organization),
            variant="X",
            image_url=IMAGE,
            attributes={"PACKAGE": {"WEIGHT": 1}},
        )
        with self.assertRaises(ValidationError) as caught:
            item.full_clean()
        self.assertIn("attributes", caught.exception.message_dict)

    def test_variants_are_the_same_model_in_the_same_organization(self):
        small = product(self.organization, variant="30ML")
        large = product(self.organization, variant="100ML")
        product(self.organization, variant="30ML", code="999")
        product(self.other, variant="50ML")
        self.assertEqual(list(small.variants()), [large])


class CatalogAdminTests(TestCase):
    def test_a_model_page_creates_its_variants_with_their_skus(self):
        organization = Organization.objects.create(name="Test Seller")
        perfumes = Category.objects.create(organization=organization, code="PERF", name="Perfumes")
        only = Brand(organization=organization, code="only", name="Only")
        only.full_clean()
        only.save()
        admin = get_user_model().objects.create_superuser("root", "root@example.com", "pw")
        self.client.force_login(admin)
        response = self.client.post(
            "/admin/catalog/productmodel/add/",
            {
                "organization": organization.pk,
                "category": perfumes.pk,
                "brand": only.pk,
                "code": "885",
                "name": "Only 885 Eau de Parfum",
                "condition": "new",
                "description": "",
                "products-TOTAL_FORMS": "2",
                "products-INITIAL_FORMS": "0",
                "products-MIN_NUM_FORMS": "0",
                "products-MAX_NUM_FORMS": "1000",
                "products-0-variant": "30ml",
                "products-0-image_url": IMAGE,
                "products-0-currency": "ARS",
                "products-0-status": "draft",
                "products-1-variant": "100ML",
                "products-1-image_url": IMAGE,
                "products-1-currency": "ARS",
                "products-1-status": "active",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            sorted(MasterProduct.objects.values_list("sku", flat=True)),
            ["PERF-ONLY-885-100ML", "PERF-ONLY-885-30ML"],
        )
        model = ProductModel.objects.get()
        for url in (
            "/admin/catalog/category/",
            "/admin/catalog/brand/add/",
            "/admin/catalog/productmodel/",
            f"/admin/catalog/productmodel/{model.pk}/change/",
            "/admin/catalog/masterproduct/",
            "/admin/catalog/masterproduct/add/",
            f"/admin/catalog/masterproduct/{MasterProduct.objects.first().pk}/change/",
        ):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        page = self.client.get(f"/admin/catalog/productmodel/{model.pk}/change/")
        self.assertContains(page, "PERF-ONLY-885")


class CodesMigrationTests(TestCase):
    def test_the_codes_migration_stops_when_products_exist(self):
        from importlib import import_module

        from django.apps import apps

        guard = import_module("catalog.migrations.0002_codes").require_empty_catalog
        guard(apps, None)
        product(Organization.objects.create(name="Test Seller"))
        with self.assertRaises(RuntimeError):
            guard(apps, None)
