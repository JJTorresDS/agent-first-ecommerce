from django.test import override_settings

from accounts.models import Membership
from catalog.test_views import member
from inventory import ledger
from inventory.tests import listing

from .listing_templates import LIVE
from .listings import store_items
from .models import ListingDraft, ListingTemplate, MeliListing
from .test_listings import item
from .test_product_drafts import PerfumeDraftCase
from .tests import shared_seller

FORM = {
    "price": "15999", "quantity": "1", "category_id": "MLA1271", "use_defaults": "on",
    "condition": "new", "warranty": "seller", "warranty_days": "30",
    "attr_key": ["PERFUME_TYPE"], "attr_value": ["Eau de parfum"],
}


class TemplateDraftTests(PerfumeDraftCase):
    """A template is posted with quantity 1 and paused at once: it never sells."""

    def save_as_template(self, **fields):
        if not ListingDraft.objects.exists():
            self.create()
        draft = self.draft()
        self.client.post(f"/catalog/meli/new/{draft.pk}/", {**FORM, "template": "on", "quantity": "7", **fields})
        draft.refresh_from_db()
        return draft

    def test_a_template_posts_one_unit_is_paused_and_becomes_the_categorys_template(self):
        self.create()
        # Stock that runs out later doesn't hold a template back: it never sells.
        ledger.record(self.organization, self.product, ledger.Kind.DAMAGE, ledger.OWN, -3)
        draft = self.save_as_template()
        self.assertEqual((draft.template, draft.quantity, draft.status), (True, 1, ListingDraft.Status.READY), draft.error)
        self.client.post("/catalog/meli/new/", {"action": "post_all"})
        draft.refresh_from_db()
        self.assertEqual(draft.status, ListingDraft.Status.PUBLISHED, draft.error)
        self.assertEqual(self.meli.created[0]["available_quantity"], 1)
        self.assertEqual(self.meli.paused, ["MLA900"])
        # Paused before the description is posted.
        methods = [call for call in self.meli.calls if call[1].startswith("/items/MLA900")]
        self.assertEqual(methods[0], ("PUT", "/items/MLA900"))
        template = ListingTemplate.objects.get(organization=self.organization)
        self.assertEqual((template.category_id, template.item_id, template.state), ("MLA1271", "MLA900", "ready"))
        self.assertEqual(MeliListing.objects.get(item_id="MLA900").status, "paused")

    def test_a_template_that_wont_pause_says_its_live(self):
        draft = self.save_as_template()
        self.meli.refuse_pause = True
        self.client.post("/catalog/meli/new/", {"action": "post_all"})
        draft.refresh_from_db()
        self.assertIn(LIVE, draft.error)
        template = ListingTemplate.objects.get()
        self.assertEqual(template.state, ListingTemplate.State.FAILED)
        self.assertIn(LIVE, template.error)

    @override_settings(MISTRAL_API_KEY="test-key")
    def test_a_template_draft_has_no_ai_buttons(self):
        self.create()
        draft = self.draft()
        self.assertContains(self.client.get(f"/catalog/meli/new/{draft.pk}/"), "Fill with AI")
        draft = self.save_as_template()
        page = self.client.get(f"/catalog/meli/new/{draft.pk}/")
        self.assertNotContains(page, "Fill with AI")
        self.assertContains(page, "Template for this category")


class MakeTemplateTests(PerfumeDraftCase):
    def setUp(self):
        super().setUp()
        self.row = listing(self.organization, "MLA900", title="Perfume 885", category_id="MLA1271", status="active")

    def make(self, row=None, action="template"):
        return self.client.post("/catalog/meli/", {"action": action, "listing": (row or self.row).pk})

    def test_making_a_template_pauses_it_first_and_one_per_category(self):
        self.make()
        template = ListingTemplate.objects.get()
        self.assertEqual((template.item_id, template.state), ("MLA900", ListingTemplate.State.READY))
        self.assertEqual(self.meli.paused, ["MLA900"])
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "paused")
        page = self.client.get("/catalog/meli/")
        self.assertContains(page, "Template · MLA1271")
        self.assertContains(page, "Stop using as template")
        other = listing(self.organization, "MLA901", title="Perfume 900", category_id="MLA1271", status="paused")
        self.make(other)
        self.assertEqual(list(ListingTemplate.objects.values_list("item_id", flat=True)), ["MLA901"])
        self.make(other, action="untemplate")
        self.assertFalse(ListingTemplate.objects.exists())
        # Stopping only removes the mark: no call to Mercado Libre, so nothing is activated.
        self.assertEqual(self.meli.calls[-1], ("PUT", "/items/MLA901"))

    def test_a_refused_pause_leaves_it_not_a_template_with_the_reason(self):
        self.meli.refuse_pause = True
        self.make()
        template = ListingTemplate.objects.get()
        self.assertEqual(template.state, ListingTemplate.State.FAILED)
        self.assertIn("Not paused, so not a template", template.error)
        self.assertContains(self.client.get("/catalog/meli/"), "Not a template yet")

    def test_the_sync_pauses_a_template_found_active_again(self):
        self.make()
        self.meli.paused.clear()
        store_items(self.organization, {"MLA900": item("MLA900", status="active", title="Perfume 885")})
        self.assertEqual(self.meli.paused, ["MLA900"])
        self.assertEqual(MeliListing.objects.get(item_id="MLA900").status, "paused")
        self.assertIn("paused again", ListingTemplate.objects.get().note)

    def test_other_organizations_and_contributors_cant_mark(self):
        other = shared_seller("Other", user_id="777")
        theirs = listing(other, "MLA777", category_id="MLA1271")
        self.assertEqual(self.make(theirs).status_code, 404)
        self.client.force_login(member(self.organization, role=Membership.Role.CONTRIBUTOR, username="bea"))
        self.assertEqual(self.make().status_code, 403)
        self.assertFalse(ListingTemplate.objects.exists())
        self.assertEqual(self.meli.paused, [])
