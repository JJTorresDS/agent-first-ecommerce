import json
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlparse

from django.test import TestCase

from accounts.models import Membership
from catalog.models import MasterProduct
from catalog.test_views import member
from catalog.tests import product
from inventory import ledger
from inventory.tests import listing

from .models import ListingChange, ListingTemplate, MeliListing
from .tests import TokenResponse, shared_seller


class ListingStatusTests(TestCase):
    """Selected listings are paused, reactivated or closed on Mercado Libre, in the worker; refusals show per row."""

    def setUp(self):
        self.organization = shared_seller("Molihaus")
        self.client.force_login(member(self.organization))
        self.calls = []
        self.refuse = set()
        self.first = listing(self.organization, "MLA1", variation_id="11", title="Perfume A", status="active")
        listing(self.organization, "MLA1", variation_id="12", title="Perfume A", status="active")
        self.second = listing(self.organization, "MLA2", title="Perfume B", status="paused")
        patcher = patch("meli.client.urlopen", side_effect=self.api)
        patcher.start()
        self.addCleanup(patcher.stop)

    def api(self, request, timeout=15):
        url = urlparse(request.full_url)
        if request.get_method() == "PUT":
            item = url.path.split("/")[2]
            if item in self.refuse:
                body = json.dumps({"message": "Item has stock in Full", "error": "validation_error", "status": 400})
                raise HTTPError(request.full_url, 400, "Bad Request", {}, BytesIO(body.encode()))
            self.calls.append((item, json.loads(request.data)["status"]))
            return TokenResponse({"id": item})
        return TokenResponse([])

    def act(self, action, *rows, reason="out_of_stock", note=""):
        return self.client.post(
            "/catalog/meli/",
            {"action": action, "selected": [row.pk for row in rows], "reason": reason, "note": note},
            follow=True,
        )

    def test_an_item_with_variations_is_changed_once(self):
        response = self.act("pause_selected", self.first, self.second)
        self.assertContains(response, "Pausing 2 listings on Mercado Libre.")
        self.assertEqual(sorted(self.calls), [("MLA1", "paused"), ("MLA2", "paused")])
        self.assertEqual(set(MeliListing.objects.values_list("status", flat=True)), {"paused"})

    def test_a_refusal_shows_on_its_row_and_others_go_ahead(self):
        self.refuse = {"MLA1"}
        response = self.act("close_selected", self.first, self.second)
        self.assertEqual(self.calls, [("MLA2", "closed")])
        self.assertEqual(MeliListing.objects.get(pk=self.first.pk).action_error, "Couldn't close it: Item has stock in Full")
        self.assertContains(response, "Item has stock in Full")
        # Closed listings leave the list unless asked for.
        self.assertNotContains(response, "Perfume B")
        self.assertContains(self.client.get("/catalog/meli/", {"status": "closed"}), "Perfume B")

    def test_a_template_is_never_reactivated_and_closing_it_drops_the_mark(self):
        ListingTemplate.objects.create(
            organization=self.organization, category_id="MLA1271", item_id="MLA2", state=ListingTemplate.State.READY
        )
        self.act("activate_selected", self.second)
        self.assertEqual(self.calls, [])
        self.assertIn("never reactivates", MeliListing.objects.get(pk=self.second.pk).action_error)
        self.act("close_selected", self.second)
        self.assertFalse(ListingTemplate.objects.exists())

    def test_only_editors_and_only_their_listings(self):
        other = shared_seller("Other")
        foreign = listing(other, "MLA9", title="Theirs", status="active")
        self.act("pause_selected", foreign)
        self.assertEqual(self.calls, [])
        self.client.force_login(member(self.organization, role=Membership.Role.CONTRIBUTOR, username="cid"))
        self.assertEqual(self.client.post(
            "/catalog/meli/", {"action": "pause_selected", "selected": [self.first.pk], "reason": "seasonal"}
        ).status_code, 403)
        self.assertNotContains(self.client.get("/catalog/meli/"), 'form="bulk-listings"')
        self.assertNotContains(self.client.get("/catalog/meli/"), "data-status-dialog data-action")
        self.assertFalse(ListingChange.objects.exists())

    def test_each_item_logs_who_why_and_how_it_went(self):
        self.act("pause_selected", self.first, self.second, reason="other", note="Restocking")
        changes = {row.item_id: row for row in ListingChange.objects.all()}
        # MLA1's two variations are one item: one change.
        self.assertEqual(sorted(changes), ["MLA1", "MLA2"])
        first = changes["MLA1"]
        self.assertEqual((first.old_value, first.new_value, first.reason, first.note), ("active", "paused", "other", "Restocking"))
        self.assertEqual(first.user.username, "ada")
        self.assertEqual(first.state, ListingChange.State.DONE)
        self.assertIsNotNone(first.finished_at)
        self.assertEqual(first.title, "Perfume A")
        page = self.client.get("/catalog/meli/?status=all")
        self.assertContains(page, "View history")
        self.assertNotContains(page, "Paused by")
        history = self.client.get("/catalog/meli/history/MLA1/")
        self.assertContains(history, "Restocking")
        self.assertContains(history, "active → paused")

    def test_only_the_waiting_cell_polls_and_gets_its_answer_when_done(self):
        self.assertNotContains(self.client.get("/catalog/meli/?status=all"), 'hx-trigger="every 2s"')
        ListingChange.objects.create(
            organization=self.organization, item_id="MLA2", new_value="active", reason="seasonal"
        )
        page = self.client.get("/catalog/meli/?status=all")
        self.assertContains(page, f'hx-get="/catalog/meli/row/{self.second.pk}/"', count=1)
        self.assertContains(page, "Reactivating…")
        url = f"/catalog/meli/row/{self.second.pk}/"
        self.assertEqual(self.client.get(url).status_code, 204)
        MeliListing.objects.filter(pk=self.second.pk).update(status="active")
        ListingChange.objects.update(state=ListingChange.State.DONE)
        cell = self.client.get(url)
        self.assertContains(cell, f'id="listing-row-{self.second.pk}"')
        self.assertContains(cell, "View history")
        self.assertNotContains(cell, "Reactivating…")
        self.assertNotContains(cell, "every 2s")
        self.assertContains(cell, 'data-action="pause_selected"')
        foreign = listing(shared_seller("Other"), "MLA9", title="Theirs", status="active")
        self.assertEqual(self.client.get(f"/catalog/meli/row/{foreign.pk}/").status_code, 404)

    def test_a_row_pauses_alone(self):
        self.assertContains(self.client.get("/catalog/meli/"), f'data-action="pause_selected" data-listing="{self.first.pk}"')
        self.act("pause_selected", self.first, reason="price_review")
        self.assertEqual(self.calls, [("MLA1", "paused")])
        self.assertEqual(ListingChange.objects.get().reason, "price_review")

    def test_no_reason_no_change(self):
        response = self.act("pause_selected", self.first, reason="")
        self.assertContains(response, "Pick a reason first")
        response = self.act("pause_selected", self.first, reason="other", note="  ")
        self.assertContains(response, "Say why in the note")
        self.act("pause_selected", self.first, reason="made_up")
        self.assertEqual(self.calls, [])
        self.assertFalse(ListingChange.objects.exists())
        self.assertEqual(MeliListing.objects.get(pk=self.first.pk).status, "active")

    def test_refusals_and_templates_fail_their_change(self):
        self.refuse = {"MLA1"}
        self.act("close_selected", self.first)
        change = ListingChange.objects.get(item_id="MLA1")
        self.assertEqual(change.state, ListingChange.State.FAILED)
        self.assertEqual(change.error, "Couldn't close it: Item has stock in Full")
        ListingTemplate.objects.create(
            organization=self.organization, category_id="MLA1271", item_id="MLA2", state=ListingTemplate.State.READY
        )
        self.act("activate_selected", self.second)
        change = ListingChange.objects.get(item_id="MLA2")
        self.assertEqual(change.state, ListingChange.State.FAILED)
        self.assertIn("never reactivates", change.error)

    def test_another_organizations_changes_stay_invisible(self):
        other = shared_seller("Other")
        foreign = listing(other, "MLA1", title="Theirs", status="active")
        ListingChange.objects.create(
            organization=other, item_id="MLA1", new_value="paused", reason="other", note="Their secret", state="done"
        )
        self.act("pause_selected", foreign)
        self.assertEqual(ListingChange.objects.filter(organization=self.organization).count(), 0)
        self.assertNotContains(self.client.get("/catalog/meli/history/MLA1/"), "Their secret")
        self.assertNotContains(self.client.get("/catalog/meli/"), "View history")


class SelectedRowsTests(TestCase):
    """Lists delete or archive what's ticked; rows something still needs stay, each with its reason."""

    def setUp(self):
        self.organization = shared_seller("Molihaus")
        self.client.force_login(member(self.organization))
        self.free = product(self.organization, variant="30ML")
        self.used = product(self.organization, variant="50ML")
        ledger.start(self.organization)
        ledger.record(self.organization, self.used, ledger.Kind.OPENING, ledger.OWN, 2)

    def test_products_delete_what_they_can_and_say_why_not(self):
        page = self.client.get("/catalog/")
        self.assertContains(page, 'id="bulk-products"')
        self.assertContains(page, f'value="{self.free.pk}" form="bulk-products"')
        response = self.client.post("/catalog/", {"action": "delete_selected", "selected": [self.free.pk, self.used.pk]}, follow=True)
        self.assertContains(response, "1 deleted.")
        self.assertContains(response, f"Not deleted: {self.used.sku} (1 stock movement)")
        self.assertEqual(list(MasterProduct.objects.values_list("pk", flat=True)), [self.used.pk])
        self.client.post("/catalog/", {"action": "archive_selected", "selected": [self.used.pk, "not-a-uuid"]})
        self.assertEqual(MasterProduct.objects.get().status, MasterProduct.Status.ARCHIVED)

    def test_another_organizations_rows_are_never_touched(self):
        other = product(shared_seller("Other"), variant="75ML")
        self.client.post("/catalog/", {"action": "delete_selected", "selected": [other.pk]})
        self.assertTrue(MasterProduct.objects.filter(pk=other.pk).exists())
