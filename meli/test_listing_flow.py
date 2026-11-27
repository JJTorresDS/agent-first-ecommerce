"""Product page → draft → Post → back to the product, and New model's starts."""

from accounts.models import Organization

from .draft_views import LISTING_SENT
from .models import ListingDraft, MeliListingProspect
from .test_product_drafts import PerfumeDraftCase
from .test_prospects import prospect_for
from inventory import ledger

READY_FORM = {
    "price": "15999", "quantity": "1", "category_id": "MLA1271", "use_defaults": "on",
    "condition": "new", "warranty": "seller", "warranty_days": "30",
    "attr_key": ["PERFUME_TYPE"], "attr_value": ["Eau de parfum"],
}


class ProductPageFlowTests(PerfumeDraftCase):
    def actions(self):
        return self.client.get(f"/catalog/meli/products/{self.product.pk}/")

    def test_a_product_without_stock_offers_fill_in_instead_of_a_dead_end(self):
        ledger.record(self.organization, self.product, ledger.Kind.DAMAGE, ledger.OWN, -3)
        actions = self.actions()
        self.assertContains(actions, "No stock in the Depósito to list.")
        self.assertContains(actions, f'hx-get="/catalog/meli/products/{self.product.pk}/ready/"')
        self.assertContains(actions, 'id="ready-dialog"')
        # The dialog's save reloads the product page, which then shows the button.
        response = self.client.post(f"/catalog/meli/products/{self.product.pk}/ready/", {
            "weight_g": "310", "height_cm": "17", "width_cm": "7.5", "length_cm": "3", "units": "2",
        })
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["HX-Refresh"], "true")
        self.assertContains(self.actions(), "Create Mercado Libre listing")

    def test_create_listing_starts_from_the_linked_scraped_listing(self):
        scraped = prospect_for(self.organization, product=self.product, category_id="MLA1271", category_path="Belleza > Perfumes")
        actions = self.actions()
        self.assertContains(actions, f'name="prospect" value="{scraped.pk}"')
        response = self.client.post(f"/catalog/meli/products/{self.product.pk}/draft/", {"kind": "product", "prospect": scraped.pk})
        draft = self.draft()
        self.assertRedirects(response, f"/catalog/meli/new/{draft.pk}/?prospect={scraped.pk}", fetch_redirect_response=False)
        self.assertEqual(draft.category_id, "MLA1271")
        self.assertEqual(draft.price, self.product.list_price)
        scraped.refresh_from_db()
        self.assertEqual(scraped.draft, draft)
        self.assertEqual(scraped.status, MeliListingProspect.Status.DRAFTED)

    def test_another_organizations_scraped_listing_is_ignored(self):
        other = Organization.objects.create(name="Other")
        theirs = prospect_for(other, category_id="MLA9999")
        response = self.client.post(f"/catalog/meli/products/{self.product.pk}/draft/", {"kind": "product", "prospect": theirs.pk})
        draft = self.draft()
        self.assertRedirects(response, f"/catalog/meli/new/{draft.pk}/", fetch_redirect_response=False)
        self.assertEqual(draft.category_id, "MLA1271")
        theirs.refresh_from_db()
        self.assertIsNone(theirs.draft_id)

    def test_save_and_check_stays_on_the_draft_and_post_goes_back_to_the_product(self):
        self.create()
        draft = self.draft()
        response = self.client.post(f"/catalog/meli/new/{draft.pk}/", READY_FORM)
        self.assertRedirects(response, f"/catalog/meli/new/{draft.pk}/")
        page = self.client.get(f"/catalog/meli/new/{draft.pk}/")
        self.assertContains(page, "Ready to post.")
        self.assertContains(page, f'href="/catalog/products/{self.product.pk}/"')
        response = self.client.post(f"/catalog/meli/new/{draft.pk}/", {"action": "post"})
        self.assertRedirects(response, f"/catalog/products/{self.product.pk}/", fetch_redirect_response=False)
        draft.refresh_from_db()
        self.assertIn(draft.status, (ListingDraft.Status.PUBLISHED, ListingDraft.Status.QUEUED, ListingDraft.Status.PUBLISHING))
        self.assertEqual(self.client.session[LISTING_SENT], str(self.product.pk))

    def test_post_needs_a_ready_draft(self):
        self.create()
        draft = self.draft()
        self.assertEqual(draft.status, ListingDraft.Status.NEEDS_FIX)
        response = self.client.post(f"/catalog/meli/new/{draft.pk}/", {"action": "post"})
        self.assertRedirects(response, f"/catalog/meli/new/{draft.pk}/", fetch_redirect_response=False)
        draft.refresh_from_db()
        self.assertEqual(draft.status, ListingDraft.Status.NEEDS_FIX)

    def test_the_state_poll_answers_nothing_while_the_worker_is_busy(self):
        self.create()
        draft = self.draft()
        ListingDraft.objects.filter(pk=draft.pk).update(status=ListingDraft.Status.CHECKING)
        response = self.client.get(f"/catalog/meli/new/{draft.pk}/?poll=state", HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 204)
        ListingDraft.objects.filter(pk=draft.pk).update(status=ListingDraft.Status.READY)
        self.assertContains(self.client.get(f"/catalog/meli/new/{draft.pk}/?poll=state"), 'id="draft-state"')

    def test_the_card_shows_a_draft_in_progress_and_polls_while_posting(self):
        self.create()
        draft = self.draft()
        ListingDraft.objects.filter(pk=draft.pk).update(status=ListingDraft.Status.QUEUED)
        actions = self.actions()
        self.assertContains(actions, "Posting to Mercado Libre…")
        self.assertContains(actions, f"/catalog/meli/products/{self.product.pk}/progress/?status=queued")
        url = f"/catalog/meli/products/{self.product.pk}/progress/"
        self.assertEqual(self.client.get(url, {"status": "queued"}).status_code, 204)
        ListingDraft.objects.filter(pk=draft.pk).update(status=ListingDraft.Status.PUBLISHED)
        response = self.client.get(url, {"status": "queued"})
        self.assertEqual(response["HX-Refresh"], "true")

    def test_after_posting_the_card_offers_the_next_scraped_listing_of_this_organization_only(self):
        waiting = prospect_for(self.organization, external_id="MLA1")
        prospect_for(Organization.objects.create(name="Other"), external_id="MLA2")
        self.assertNotContains(self.actions(), "Next scraped listing")
        session = self.client.session
        session[LISTING_SENT] = str(self.product.pk)
        session.save()
        actions = self.actions()
        self.assertContains(actions, f'href="/catalog/import/?prospect={waiting.pk}"')
        self.assertContains(actions, "1 waiting for a product")


class NewModelTests(PerfumeDraftCase):
    def test_new_model_opens_on_scraped_listings_while_some_wait(self):
        self.assertRedirects(self.client.get("/catalog/new/"), "/catalog/models/new/")
        prospect_for(Organization.objects.create(name="Other"), external_id="MLA2")
        self.assertRedirects(self.client.get("/catalog/new/"), "/catalog/models/new/")
        prospect_for(self.organization, external_id="MLA1")
        self.assertRedirects(self.client.get("/catalog/new/"), "/catalog/import/?start=scraped")

    def test_the_three_starts_are_tabs_on_both_pages(self):
        for url in ("/catalog/models/new/", "/catalog/import/?start=scraped", "/catalog/import/"):
            page = self.client.get(url)
            self.assertContains(page, "From a scraped listing")
            self.assertContains(page, "From a link")
            self.assertContains(page, 'href="/catalog/models/new/"')


class DraftQuantityTests(PerfumeDraftCase):
    """The drafts list's quantity box."""

    def save(self, draft, quantity):
        return self.client.post(f"/catalog/meli/new/{draft.pk}/quantity/", {"quantity": quantity})

    def ready_draft(self):
        self.create()
        draft = self.draft()
        ListingDraft.objects.filter(pk=draft.pk).update(status=ListingDraft.Status.READY)
        draft.refresh_from_db()
        return draft

    def test_the_list_offers_a_box_and_saving_keeps_the_draft_ready(self):
        draft = self.ready_draft()
        self.assertContains(self.client.get("/catalog/meli/new/"), f'id="draft-qty-input-{draft.pk}"')
        response = self.save(draft, 3)
        self.assertContains(response, "Saved")
        draft.refresh_from_db()
        self.assertEqual((draft.quantity, draft.status), (3, ListingDraft.Status.READY))

    def test_never_above_the_depósito_stock(self):
        draft = self.ready_draft()
        self.assertContains(self.save(draft, 4), "Only 3 in stock in the Depósito.")
        self.assertContains(self.save(draft, 0), "At least 1.")
        self.assertContains(self.save(draft, "x"), "At least 1.")
        draft.refresh_from_db()
        self.assertEqual(draft.quantity, 1)

    def test_a_posting_draft_or_another_organizations_one_does_not_change(self):
        draft = self.ready_draft()
        ListingDraft.objects.filter(pk=draft.pk).update(status=ListingDraft.Status.QUEUED)
        self.assertEqual(self.save(draft, 2).status_code, 403)
        other = Organization.objects.create(name="Other")
        theirs = ListingDraft.objects.create(organization=other, kind=ListingDraft.Kind.PRODUCT, price=1, quantity=1)
        self.assertEqual(self.save(theirs, 2).status_code, 404)
        theirs.refresh_from_db()
        self.assertEqual(theirs.quantity, 1)
