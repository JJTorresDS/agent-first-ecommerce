from .models import ListingDraft
from .test_drafts import DraftTestCase
from .tests import shared_seller


class DraftDeleteTests(DraftTestCase):
    def test_selected_drafts_go_except_those_being_posted_and_other_organizations(self):
        self.add()
        first = self.draft()
        busy = ListingDraft.objects.create(
            organization=self.organization, product=self.product, price=1, quantity=1,
            status=ListingDraft.Status.PUBLISHING,
        )
        other = shared_seller("Other")
        foreign = ListingDraft.objects.create(organization=other, product=self.product, price=1, quantity=1)
        response = self.client.post(
            "/catalog/meli/new/", {"action": "delete", "draft": [first.pk, busy.pk, foreign.pk]}, follow=True
        )
        self.assertContains(response, "1 draft deleted.")
        self.assertEqual(
            set(ListingDraft.objects.values_list("pk", flat=True)), {busy.pk, foreign.pk}
        )

    def test_the_draft_page_asks_before_deleting(self):
        self.add()
        draft = self.draft()
        page = self.client.get(f"/catalog/meli/new/{draft.pk}/")
        self.assertContains(page, 'data-dialog="delete-dialog"')
        self.client.post(f"/catalog/meli/new/{draft.pk}/", {"action": "delete"})
        self.assertFalse(ListingDraft.objects.exists())
