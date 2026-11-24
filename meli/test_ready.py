import json
from decimal import Decimal

from accounts.models import Membership
from catalog.test_views import member
from catalog.tests import product
from inventory import ledger

from .models import ListingDraft
from .test_product_drafts import PerfumeDraftCase
from .test_prospects import PAGE_URL, prospect_for
from .tests import shared_seller

PACKAGE = {"weight_g": "310", "length_cm": "3", "width_cm": "7.5", "height_cm": "17"}


class FillInTests(PerfumeDraftCase):
    """The Fill in dialog saves a product's package and Depósito stock without leaving the page."""

    def setUp(self):
        super().setUp()
        self.product.weight_g = None
        self.product.height_cm = None
        self.product.save()
        ledger.record(self.organization, self.product, ledger.Kind.DAMAGE, ledger.OWN, -3)
        self.prospect = prospect_for(self.organization, product=self.product)
        self.url = f"/catalog/meli/products/{self.product.pk}/ready/"

    def test_the_scraped_tab_offers_fill_in_and_a_save_makes_the_row_listable(self):
        page = self.client.get(PAGE_URL)
        self.assertContains(page, "Fill in the package weight and size first.")
        self.assertContains(page, f'hx-get="{self.url}"', html=False)
        dialog = self.client.get(self.url)
        self.assertContains(dialog, 'name="weight_g"', html=False)
        self.assertContains(dialog, 'name="units"', html=False)
        self.assertNotContains(dialog, 'name="list_price"', html=False)
        response = self.client.post(self.url, {**PACKAGE, "units": "4"})
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["HX-Refresh"], "true")
        self.product.refresh_from_db()
        self.assertEqual((self.product.weight_g, self.product.height_cm), (310, Decimal("17.0")))
        self.assertEqual(ledger.on_hand(self.product)[ledger.OWN], 4)
        page = self.client.get(PAGE_URL)
        self.assertContains(page, f"/catalog/meli/new/scraped/{self.prospect.pk}/")
        self.assertNotContains(page, "Fill in the package weight and size first.")

    def test_a_missing_field_is_an_error_and_nothing_is_saved(self):
        response = self.client.post(self.url, {**PACKAGE, "weight_g": "", "units": "4"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This field is required.")
        self.product.refresh_from_db()
        self.assertIsNone(self.product.height_cm)
        self.assertEqual(ledger.on_hand(self.product)[ledger.OWN], 0)

    def test_the_draft_page_saves_without_reloading(self):
        draft = ListingDraft.objects.create(
            organization=self.organization, product=self.product, price=Decimal("15999"), quantity=1,
            category_id="MLA1271", status=ListingDraft.Status.NEEDS_FIX,
        )
        page = self.client.get(f"/catalog/meli/new/{draft.pk}/")
        self.assertContains(page, "Before posting, the product needs the package weight and size and stock in the Depósito.")
        response = self.client.post(self.url, {**PACKAGE, "units": "2", "place": "draft"})
        self.assertEqual(response.status_code, 204)
        ready = json.loads(response["HX-Trigger"])["product-ready"]
        self.assertEqual((ready["available"], ready["done"]), (2, True))
        self.assertNotContains(self.client.get(f"/catalog/meli/new/{draft.pk}/"), 'id="ready-notice"', html=False)

    def test_another_organizations_product_is_not_found_or_changed(self):
        other = shared_seller("Other", user_id="777")
        theirs = product(other, variant="30ML")
        url = f"/catalog/meli/products/{theirs.pk}/ready/"
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, {**PACKAGE, "units": "5"}).status_code, 404)
        theirs.refresh_from_db()
        self.assertIsNone(theirs.weight_g)
        self.assertEqual(ledger.on_hand(theirs)[ledger.OWN], 0)

    def test_only_owners_and_admins_fill_in(self):
        viewer = member(self.organization, role=Membership.Role.VIEWER, username="grace")
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, {**PACKAGE, "units": "5"}).status_code, 403)
        self.product.refresh_from_db()
        self.assertIsNone(self.product.weight_g)
