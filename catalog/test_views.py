from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from accounts.models import AppAccess, Membership, Organization, Profile
from purchasing.models import Purchase, PurchaseItem, Supplier

from .models import Brand, Category, MasterProduct, ProductModel
from .tests import IMAGE, product, product_model

User = get_user_model()


def member(organization, role=Membership.Role.OWNER, apps=(AppAccess.App.CATALOG,), username="ada"):
    user = User.objects.create_user(username, email=f"{username}@example.com")
    Membership.objects.create(user=user, organization=organization, role=role)
    Profile.objects.create(user=user, organization=organization)
    for app in apps:
        AppAccess.objects.create(user=user, organization=organization, app=app)
    return user


class CatalogAccessTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Seller")
        self.item = product(self.organization, variant="30ML")

    def test_without_catalog_access_every_page_is_closed(self):
        self.client.force_login(member(self.organization, apps=()))
        for url in (
            "/catalog/",
            "/catalog/models/",
            f"/catalog/models/{self.item.product_model_id}/",
            f"/catalog/products/{self.item.pk}/",
            "/catalog/codes/",
        ):
            self.assertEqual(self.client.get(url).status_code, 403, url)
        page = self.client.get("/inbox/")
        self.assertNotContains(page, 'href="/catalog/"')

    def test_a_contributor_reads_but_cannot_change_the_catalog(self):
        self.client.force_login(member(self.organization, role=Membership.Role.CONTRIBUTOR))
        listing = self.client.get("/catalog/")
        self.assertContains(listing, "PERF-ONLY-885-30ML")
        self.assertNotContains(listing, "New model")
        page = self.client.get(f"/catalog/products/{self.item.pk}/")
        self.assertContains(page, "Only owners and admins can change the catalog.")
        self.assertNotContains(page, "Save product")
        for url, data in (
            (f"/catalog/products/{self.item.pk}/", {"title": "Changed"}),
            ("/catalog/codes/", {"kind": "brand", "brand-code": "CK", "brand-name": "CK"}),
            ("/catalog/models/new/", {"action": "model"}),
        ):
            self.assertEqual(self.client.post(url, data).status_code, 403, url)
        self.item.refresh_from_db()
        self.assertNotEqual(self.item.title, "Changed")
        self.assertFalse(Brand.objects.filter(code="CK").exists())

    def test_the_sidebar_shows_products_entries_by_access(self):
        user = member(self.organization, apps=(AppAccess.App.CATALOG,))
        self.client.force_login(user)
        page = self.client.get("/catalog/")
        self.assertContains(page, 'href="/catalog/"')
        self.assertNotContains(page, 'href="/purchases/"')
        AppAccess.objects.create(user=user, organization=self.organization, app=AppAccess.App.PURCHASING)
        self.assertContains(self.client.get("/catalog/"), 'href="/purchases/"')


class CatalogTenancyTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Seller")
        self.other = Organization.objects.create(name="Other")
        self.client.force_login(member(self.organization))
        self.theirs = product(self.other, variant="SECRET")

    def test_another_organizations_rows_are_never_shown_or_opened(self):
        product(self.organization, variant="30ML")
        listing = self.client.get("/catalog/?q=PERF")
        self.assertContains(listing, "PERF-ONLY-885-30ML")
        self.assertNotContains(listing, "SECRET")
        self.assertEqual(self.client.get(f"/catalog/products/{self.theirs.pk}/").status_code, 404)
        self.assertEqual(self.client.get(f"/catalog/models/{self.theirs.product_model_id}/").status_code, 404)
        models = self.client.get("/catalog/models/")
        self.assertNotContains(models, str(self.theirs.product_model_id) + "/")
        Brand.objects.create(organization=self.other, code="HIDDEN", name="Their own brand")
        codes = self.client.get("/catalog/codes/")
        self.assertContains(codes, "Brand ONLY")
        self.assertNotContains(codes, "HIDDEN")
        self.assertNotContains(codes, "Their own brand")
        self.assertNotContains(self.client.get("/catalog/"), "Their own brand")

    def test_a_model_cannot_use_another_organizations_category_or_brand(self):
        theirs = self.theirs.product_model
        response = self.client.post(
            "/catalog/models/new/",
            {"action": "model", "category": theirs.category_id, "brand": theirs.brand_id, "code": "1", "name": "X", "condition": "new"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Select a valid choice")
        self.assertFalse(ProductModel.objects.filter(organization=self.organization).exists())


class CatalogFlowTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Seller")
        self.user = member(self.organization)
        self.client.force_login(self.user)

    def test_codes_then_a_model_then_its_variants(self):
        self.client.post("/catalog/codes/", {"kind": "category", "category-code": "perf", "category-name": "Perfumes"})
        self.client.post("/catalog/codes/", {"kind": "brand", "brand-code": "ONLY", "brand-name": "Only"})
        duplicate = self.client.post("/catalog/codes/", {"kind": "brand", "brand-code": "only", "brand-name": "Other"})
        self.assertContains(duplicate, "already exists")
        bad = self.client.post("/catalog/codes/", {"kind": "brand", "brand-code": "ON-LY", "brand-name": "Bad"})
        self.assertContains(bad, "capital letters or digits")
        category = Category.objects.get(organization=self.organization)
        brand = Brand.objects.get(organization=self.organization)
        self.assertEqual(category.code, "PERF")

        created = self.client.post(
            "/catalog/models/new/",
            {"action": "model", "category": category.pk, "brand": brand.pk, "code": "885", "name": "Only 885 Eau de Parfum", "condition": "new"},
        )
        model = ProductModel.objects.get()
        self.assertRedirects(created, f"/catalog/models/{model.pk}/")
        for variant in ("30ml", "100ML"):
            added = self.client.post(
                f"/catalog/models/{model.pk}/",
                {"action": "variant", "variant-variant": variant, "variant-image_url": IMAGE, "variant-currency": "ARS", "variant-status": "active"},
            )
            self.assertEqual(added.status_code, 302)
        again = self.client.post(
            f"/catalog/models/{model.pk}/",
            {"action": "variant", "variant-variant": "30ML", "variant-image_url": IMAGE, "variant-currency": "ARS", "variant-status": "draft"},
        )
        self.assertEqual(again.status_code, 200)
        self.assertEqual(
            sorted(MasterProduct.objects.values_list("sku", flat=True)),
            ["PERF-ONLY-885-100ML", "PERF-ONLY-885-30ML"],
        )
        page = self.client.get(f"/catalog/models/{model.pk}/")
        self.assertContains(page, "Variants use this model")
        self.assertContains(page, 'id="sku-codes"')
        locked = self.client.post(
            f"/catalog/models/{model.pk}/",
            {"action": "model", "category": category.pk, "brand": brand.pk, "code": "999", "name": "Renamed", "condition": "new"},
        )
        self.assertEqual(locked.status_code, 302)
        model.refresh_from_db()
        self.assertEqual((model.code, model.name), ("885", "Renamed"))

    def test_a_brand_name_can_change_but_a_used_code_cannot(self):
        item = product(self.organization, variant="30ML")
        brand = item.product_model.brand
        renamed = self.client.post(
            "/catalog/codes/", {"kind": "brand", "id": brand.pk, "brand-code": "ONLY", "brand-name": "Only Fragrances"}
        )
        self.assertRedirects(renamed, "/catalog/codes/")
        recoded = self.client.post(
            "/catalog/codes/", {"kind": "brand", "id": brand.pk, "brand-code": "ONLYF", "brand-name": "Only Fragrances"}
        )
        self.assertContains(recoded, "Products already use this code")
        brand.refresh_from_db()
        self.assertEqual((brand.code, brand.name), ("ONLY", "Only Fragrances"))
        item.refresh_from_db()
        self.assertEqual(item.sku, "PERF-ONLY-885-30ML")
        theirs = Brand.objects.create(organization=Organization.objects.create(name="Other"), code="THEM", name="Them")
        stolen = self.client.post(
            "/catalog/codes/", {"kind": "brand", "id": theirs.pk, "brand-code": "THEM", "brand-name": "Mine now"}
        )
        self.assertEqual(stolen.status_code, 404)

    def test_a_product_saves_its_listing_and_attributes(self):
        item = product(self.organization, variant="30ML")
        response = self.client.post(
            f"/catalog/products/{item.pk}/",
            {
                "title": "Only 885 EDP 30 ml",
                "status": "active",
                "image_url": IMAGE,
                "gtin": "7791234567890",
                "list_price": "45000",
                "currency": "ARS",
                "weight_g": "180",
                "attr_key": ["VOLUME_ML", "FRAGRANCE_NOTES", "CODE", "GIFT", ""],
                "attr_value": ["30", "vainilla; caramelo", "007", "true", "ignored"],
            },
        )
        self.assertRedirects(response, f"/catalog/products/{item.pk}/?saved=1")
        item.refresh_from_db()
        self.assertEqual(item.status, "active")
        self.assertEqual(item.weight_g, 180)
        self.assertEqual(
            item.attributes,
            {"VOLUME_ML": 30, "FRAGRANCE_NOTES": ["vainilla", "caramelo"], "CODE": "007", "GIFT": True},
        )
        page = self.client.get(f"/catalog/products/{item.pk}/")
        self.assertContains(page, 'value="vainilla; caramelo"')
        self.assertContains(page, 'value="true"')
        bad = self.client.post(
            f"/catalog/products/{item.pk}/",
            {"title": "X", "status": "active", "image_url": "ftp://x", "currency": "ARS", "attr_key": ["X" * 61], "attr_value": ["1"]},
        )
        self.assertEqual(bad.status_code, 200)
        self.assertContains(bad, "Attribute names must be text")
        item.refresh_from_db()
        self.assertEqual(item.title, "Only 885 EDP 30 ml")

    def test_filters_narrow_the_products(self):
        product(self.organization, variant="30ML", status="active")
        product(self.organization, variant="100ML")
        product(self.organization, variant="50ML", code="999")
        active = self.client.get("/catalog/?status=active", HTTP_HX_REQUEST="true")
        self.assertContains(active, "PERF-ONLY-885-30ML")
        self.assertNotContains(active, "PERF-ONLY-885-100ML")
        found = self.client.get("/catalog/?q=999")
        self.assertContains(found, "PERF-ONLY-999-50ML")
        self.assertNotContains(found, "PERF-ONLY-885-30ML")

    def test_costs_show_only_with_purchasing_access(self):
        item = product(self.organization, variant="30ML")
        supplier = Supplier.objects.create(organization=self.organization, name="Importadora Sur")
        purchase = Purchase.objects.create(organization=self.organization, supplier=supplier, purchased_on=date(2026, 10, 1))
        PurchaseItem.objects.create(purchase=purchase, product=item, quantity=3, unit_cost=Decimal("12000"))
        page = self.client.get(f"/catalog/products/{item.pk}/")
        self.assertNotContains(page, "Cost by supplier")
        self.assertNotContains(page, "Importadora Sur")
        AppAccess.objects.create(user=self.user, organization=self.organization, app=AppAccess.App.PURCHASING)
        page = self.client.get(f"/catalog/products/{item.pk}/")
        self.assertContains(page, "Cost by supplier")
        self.assertContains(page, "Importadora Sur")
        self.assertContains(page, "ARS 12000.00")
