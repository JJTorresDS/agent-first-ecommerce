from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from accounts.models import Organization

from .models import MasterProduct
from .validators import validate_attributes

IMAGE = "https://cdn.example.com/caramel-dream-100.jpg"


def product(organization, sku="CD-100", **fields):
    return MasterProduct.objects.create(
        organization=organization,
        sku=sku,
        title=fields.pop("title", "Caramel Dream Sweet Tooth 100 ml"),
        image_url=fields.pop("image_url", IMAGE),
        **fields,
    )


class MasterProductTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Seller")
        self.other = Organization.objects.create(name="Other")

    def test_a_product_saves_with_its_core_fields_and_attributes(self):
        item = product(
            self.organization,
            brand="Sabrina Carpenter",
            gtin="7791234567890",
            family="caramel-dream",
            attributes={"VOLUME_ML": 100, "FRAGRANCE_NOTES": ["vainilla", "caramelo"], "GIFT": True},
        )
        item.full_clean()
        item.refresh_from_db()
        self.assertEqual(item.status, MasterProduct.Status.DRAFT)
        self.assertEqual(item.condition, MasterProduct.Condition.NEW)
        self.assertEqual(item.currency, "ARS")
        self.assertEqual(item.attributes["FRAGRANCE_NOTES"], ["vainilla", "caramelo"])

    def test_the_image_url_is_required_and_must_be_http(self):
        for bad in ("", "ftp://cdn.example.com/a.jpg", "not a url"):
            item = MasterProduct(organization=self.organization, sku="X", title="X", image_url=bad)
            with self.assertRaises(ValidationError) as caught:
                item.full_clean()
            self.assertIn("image_url", caught.exception.message_dict)

    def test_a_sku_is_unique_per_organization_only(self):
        product(self.organization)
        product(self.other)
        with self.assertRaises(IntegrityError), transaction.atomic():
            product(self.organization)
        self.assertEqual(MasterProduct.objects.filter(sku="CD-100").count(), 2)

    def test_gtin_must_be_a_barcode_and_is_unique_when_set(self):
        for bad in ("77912345678AB", "1234567", "123456789"):
            item = MasterProduct(
                organization=self.organization, sku=bad, title="X", image_url=IMAGE, gtin=bad
            )
            with self.assertRaises(ValidationError):
                item.full_clean()
        product(self.organization, sku="A")
        product(self.organization, sku="B")
        product(self.organization, sku="C", gtin="7791234567890")
        product(self.other, sku="C", gtin="7791234567890")
        with self.assertRaises(IntegrityError), transaction.atomic():
            product(self.organization, sku="D", gtin="7791234567890")

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
            organization=self.organization,
            sku="X",
            title="X",
            image_url=IMAGE,
            attributes={"PACKAGE": {"WEIGHT": 1}},
        )
        with self.assertRaises(ValidationError) as caught:
            item.full_clean()
        self.assertIn("attributes", caught.exception.message_dict)

    def test_variants_are_the_same_family_in_the_same_organization(self):
        small = product(self.organization, sku="CD-50", family="caramel-dream")
        large = product(self.organization, sku="CD-100", family="caramel-dream")
        product(self.organization, sku="OTHER", family="vanilla")
        product(self.other, sku="CD-200", family="caramel-dream")
        loose = product(self.organization, sku="LOOSE")
        self.assertEqual(list(small.variants()), [large])
        self.assertEqual(list(loose.variants()), [])
