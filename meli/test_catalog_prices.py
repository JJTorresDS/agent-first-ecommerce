import json
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

from django.test import TestCase

from accounts.models import Membership
from catalog.test_views import member
from inventory.tests import listing

from .models import CatalogCompetition
from .tests import TokenResponse, shared_seller

ANSWERS = {
    "MLA1": {
        "item_id": "MLA1", "current_price": 1200, "currency_id": "ARS", "price_to_win": 1099.9,
        "status": "competing", "reason": ["non_competitive_price"],
        "winner": {"item_id": "MLA777", "price": 1100, "currency_id": "ARS"},
    },
    "MLA2": {
        "item_id": "MLA2", "current_price": 900, "currency_id": "ARS", "price_to_win": None,
        "status": "winning", "winner": {"item_id": "MLA2", "price": 900, "currency_id": "ARS"},
    },
}


class CatalogPriceTests(TestCase):
    """The winning price of a catalog page is read only when asked, per row or for every catalog listing."""

    def setUp(self):
        self.organization = shared_seller("Molihaus")
        self.client.force_login(member(self.organization))
        self.asked = []
        self.losing = listing(self.organization, "MLA1", title="Perfume A", status="active", catalog_listing=True,
                              price=Decimal("1200"), payload={"catalog_product_id": "MLA45601658"})
        self.winning = listing(self.organization, "MLA2", title="Perfume B", status="active", catalog_listing=True)
        self.twin = listing(self.organization, "MLA3", title="Perfume A regular", status="active",
                            payload={"item_relations": [{"id": "MLA1", "stock_relation": 1}]})
        self.closed = listing(self.organization, "MLA4", title="Old", status="closed", catalog_listing=True)
        self.plain = listing(self.organization, "MLA5", title="Plain", status="active")
        patcher = patch("meli.client.urlopen", side_effect=self.api)
        patcher.start()
        self.addCleanup(patcher.stop)

    def api(self, request, timeout=15):
        url = urlparse(request.full_url)
        if url.path.endswith("/price_to_win"):
            item = url.path.split("/")[2]
            self.asked.append(item)
            self.assertEqual(parse_qs(url.query), {"siteId": ["MLA"], "version": ["v2"]})
            if item not in ANSWERS:
                body = json.dumps({"message": "Item is not in a catalog", "error": "not_found", "status": 404})
                raise HTTPError(request.full_url, 404, "Not Found", {}, BytesIO(body.encode()))
            return TokenResponse(ANSWERS[item])
        return TokenResponse([])

    def test_nothing_is_asked_until_a_user_does(self):
        page = self.client.get("/catalog/meli/")
        self.assertEqual(self.asked, [])
        self.assertContains(page, "Get winning price", count=3)  # MLA1, MLA2 and the twin MLA3
        self.assertContains(page, "Check catalog prices")

    def test_check_all_skips_closed_and_shows_winner_and_price_to_win(self):
        response = self.client.post(
            "/catalog/meli/", {"action": "catalog_prices", "next": "/catalog/meli/?q=Perfume"}, follow=True
        )
        self.assertEqual(sorted(self.asked), ["MLA1", "MLA2"])
        self.assertContains(response, "Checking 2 catalog listings")
        self.assertEqual(response.redirect_chain[-1][0], "/catalog/meli/?q=Perfume")
        losing = CatalogCompetition.objects.get(item_id="MLA1")
        self.assertEqual((losing.status, losing.winner_item_id, losing.winner_price, losing.price_to_win),
                         ("competing", "MLA777", Decimal("1100.00"), Decimal("1099.90")))
        self.assertEqual(losing.reasons, ["non_competitive_price"])
        self.assertContains(response, "Losing")
        self.assertContains(response, "Winner ARS 1100.00")
        self.assertContains(response, "To win: ARS 1099.90")
        self.assertContains(response, "Winner (you) ARS 900.00")
        # The twin row shows its catalog listing's standing.
        self.assertContains(response, "Via MLA1")
        self.assertContains(response, "Winner ARS 1100.00", count=2)

    def test_a_row_refreshes_alone_and_polls_while_checking(self):
        CatalogCompetition.objects.create(organization=self.organization, item_id="MLA1", state="checking")
        row = self.client.post(f"/catalog/meli/row/{self.losing.pk}/")
        self.assertEqual(self.asked, ["MLA1"])
        self.assertContains(row, f'id="listing-row-{self.losing.pk}"')
        self.assertContains(row, "Winner ARS 1100.00")
        self.assertContains(row, "Refresh")
        self.assertNotContains(row, "every 2s")
        CatalogCompetition.objects.filter(item_id="MLA1").update(state="checking", requested_at=self.losing.synced_at)
        self.assertEqual(self.client.get(f"/catalog/meli/row/{self.losing.pk}/").status_code, 204)
        self.assertContains(self.client.get("/catalog/meli/"), "Checking…")

    def test_a_refusal_is_kept_on_the_row(self):
        with patch.dict(ANSWERS):
            del ANSWERS["MLA2"]
            row = self.client.post(f"/catalog/meli/row/{self.winning.pk}/")
        self.assertContains(row, "Couldn&#x27;t check: Item is not in a catalog")
        self.assertEqual(CatalogCompetition.objects.get(item_id="MLA2").state, "failed")

    def test_only_editors_and_only_their_listings(self):
        other = shared_seller("Other")
        foreign = listing(other, "MLA9", title="Theirs", status="active", catalog_listing=True)
        self.assertEqual(self.client.post(f"/catalog/meli/row/{foreign.pk}/").status_code, 404)
        CatalogCompetition.objects.create(organization=other, item_id="MLA1", state="ready", status="winning",
                                          winner_price=Decimal("1"), currency="ARS", checked_at=foreign.synced_at)
        self.assertNotContains(self.client.get("/catalog/meli/"), "Winner")
        # A plain listing's row can't ask about anything.
        self.client.post(f"/catalog/meli/row/{self.plain.pk}/")
        self.client.force_login(member(self.organization, role=Membership.Role.CONTRIBUTOR, username="cid"))
        self.assertEqual(self.client.post(f"/catalog/meli/row/{self.losing.pk}/").status_code, 403)
        self.assertEqual(self.client.post("/catalog/meli/", {"action": "catalog_prices"}).status_code, 403)
        self.assertNotContains(self.client.get("/catalog/meli/"), "Get winning price")
        self.assertEqual(self.asked, [])
        self.assertFalse(CatalogCompetition.objects.filter(organization=self.organization).exists())
