"""Mercado Libre claims: read from webhooks and Refresh, listed by deadline, warned about in the bell."""

import json
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import Organization
from catalog.test_views import member

from .claim_views import time_left
from .models import BUENOS_AIRES, MeliClaim, MeliClaimSync
from .tests import SAMPLE, WEBHOOK_SECRET, WEBHOOK_URL, TokenResponse, shared_seller

SELLER = "468424240"


def claim_payload(claim_id="5617705406", status="opened", hours=5, order="2000012345"):
    due = (timezone.now() + timedelta(hours=hours)).astimezone(BUENOS_AIRES).strftime("%Y-%m-%dT%H:%M:%S.000-03:00")
    later = (timezone.now() + timedelta(days=3)).astimezone(BUENOS_AIRES).strftime("%Y-%m-%dT%H:%M:%S.000-03:00")
    return {
        "id": int(claim_id),
        "resource": "order",
        "resource_id": int(order),
        "status": status,
        "type": "mediations",
        "stage": "claim",
        "reason_id": "PDD9939",
        "date_created": "2026-11-24T17:45:00.000-03:00",
        "last_updated": "2026-11-26T14:35:00.000-03:00",
        "players": [
            {"role": "complainant", "type": "buyer", "user_id": 99, "available_actions": []},
            {"role": "respondent", "type": "seller", "user_id": int(SELLER), "available_actions": [
                {"action": "send_message_to_complainant", "mandatory": False, "due_date": later},
                {"action": "refund", "mandatory": True, "due_date": due},
            ]},
        ],
    }


class FakeClaims:
    def __init__(self):
        self.claims = {"5617705406": claim_payload()}
        self.search = None
        self.calls = []

    def __call__(self, request, timeout=15):
        url = urlparse(request.full_url)
        self.calls.append(url.path)
        if url.path == "/post-purchase/v1/claims/search":
            query = parse_qs(url.query)
            assert query["player_role"] == ["respondent"] and query["player_user_id"] == [SELLER]
            found = self.search if self.search is not None else list(self.claims.values())
            return TokenResponse({"paging": {"total": len(found)}, "data": found})
        if url.path.startswith("/post-purchase/v1/claims/"):
            return TokenResponse(self.claims[url.path.rsplit("/", 1)[1]])
        if url.path.startswith("/orders/"):
            return TokenResponse({"id": 1, "buyer": {"nickname": "COMPRADOR1"},
                                  "order_items": [{"item": {"title": "Reloj deportivo digital-analógico"}}]})
        raise AssertionError(url.path)


@override_settings(MELI_WEBHOOK_SECRET=WEBHOOK_SECRET, MELI_CLIENT_ID=str(SAMPLE["application_id"]))
class ClaimTests(TestCase):
    def setUp(self):
        self.organization = shared_seller("Molihaus", user_id=SELLER)
        self.client.force_login(member(self.organization))
        self.meli = FakeClaims()
        patcher = patch("meli.client.urlopen", side_effect=self.meli)
        patcher.start()
        self.addCleanup(patcher.stop)

    def notify(self, resource="/post-purchase/v1/claims/5617705406"):
        return self.client.post(WEBHOOK_URL, data=json.dumps(
            {**SAMPLE, "_id": resource, "topic": "post_purchase", "actions": ["claims"], "resource": resource}
        ), content_type="application/json")

    def test_a_webhook_stores_the_claim_with_the_sellers_earliest_deadline_in_every_organization(self):
        second = shared_seller("Second shop", user_id=SELLER)
        self.assertEqual(self.notify().status_code, 200)
        rows = MeliClaim.objects.filter(claim_id="5617705406")
        self.assertEqual({row.organization_id for row in rows}, {self.organization.pk, second.pk})
        row = rows.get(organization=self.organization)
        self.assertEqual((row.status, row.claim_type, row.resource_id), ("opened", "mediations", "2000012345"))
        self.assertAlmostEqual((row.due_at - timezone.now()).total_seconds(), 5 * 3600, delta=120)
        self.assertEqual([item["action"] for item in row.actions], ["refund", "send_message_to_complainant"])
        self.assertEqual((row.title, row.buyer), ("Reloj deportivo digital-analógico", "COMPRADOR1"))
        self.assertEqual(row.sale_url(), "https://www.mercadolibre.com.ar/ventas/2000012345/detalle")

    def test_an_actions_history_webhook_reads_its_claim_and_other_topics_paths_are_ignored(self):
        self.notify("/post-purchase/v1/claims/5617705406/actions-history")
        self.assertTrue(MeliClaim.objects.filter(claim_id="5617705406").exists())
        self.notify("/post-purchase/v1/claims/notanid")
        self.assertEqual(MeliClaim.objects.count(), 1)

    def test_a_closed_claim_has_no_deadline(self):
        self.meli.claims["5617705406"] = claim_payload(status="closed")
        self.notify()
        self.assertIsNone(MeliClaim.objects.get().due_at)

    def test_the_tab_lists_open_claims_nearest_deadline_first_and_only_this_organizations(self):
        self.meli.claims["5620131422"] = claim_payload("5620131422", hours=60, order="2000099999")
        other = shared_seller("Other", user_id="555")
        MeliClaim.objects.create(organization=other, claim_id="1", status="opened", title="Not ours",
                                 due_at=timezone.now() + timedelta(hours=1), fetched_at=timezone.now())
        page = self.client.get("/meli/claims/")
        # The first visit read the open claims (Refresh in the worker).
        self.assertEqual(MeliClaimSync.objects.get(organization=self.organization).state, "ok")
        page = self.client.get("/meli/claims/")
        self.assertContains(page, "Open (2)")
        self.assertNotContains(page, "Not ours")
        body = page.content.decode()
        self.assertLess(body.index("#5617705406"), body.index("#5620131422"))
        self.assertContains(page, '<strong class="error">in 4 h</strong>', html=False)
        self.assertContains(page, "Refund, Message the buyer")
        self.assertContains(page, 'href="https://www.mercadolibre.com.ar/ventas/2000012345/detalle"')

    def test_refresh_reads_a_stored_open_claim_again_when_the_search_no_longer_lists_it(self):
        self.client.get("/meli/claims/")
        self.meli.search = []
        self.meli.claims["5617705406"] = claim_payload(status="closed")
        self.client.post("/meli/claims/")
        row = MeliClaim.objects.get()
        self.assertEqual(row.status, "closed")
        self.assertContains(self.client.get("/meli/claims/?status=closed"), "#5617705406")
        self.assertContains(self.client.get("/meli/claims/"), "No open claims.")

    def test_the_bell_and_the_sidebar_warn_about_claims_of_this_organization_only(self):
        self.notify()
        page = self.client.get("/catalog/")
        self.assertContains(page, "1 Mercado Libre claim waits for your answer: the first by")
        self.assertContains(page, 'title="Claims waiting for your answer">1</span>', html=False)
        MeliClaim.objects.filter(organization=self.organization).delete()
        other = shared_seller("Other", user_id="555")
        MeliClaim.objects.create(organization=other, claim_id="1", status="opened",
                                 due_at=timezone.now() + timedelta(hours=1), fetched_at=timezone.now())
        self.assertNotContains(self.client.get("/catalog/"), "claim waits")

    def test_without_a_connection_the_tab_says_so(self):
        lonely = Organization.objects.create(name="Lonely")
        self.client.force_login(member(lonely, username="lonely"))
        self.assertContains(self.client.get("/meli/claims/"), "Mercado Libre isn't connected")
        self.assertFalse(MeliClaimSync.objects.filter(organization=lonely).exists())

    def test_time_left(self):
        now = timezone.now()
        self.assertEqual(time_left(now - timedelta(minutes=1), now), "Overdue")
        self.assertEqual(time_left(now + timedelta(minutes=20), now), "in 20 min")
        self.assertEqual(time_left(now + timedelta(hours=30), now), "in 30 h")
        self.assertEqual(time_left(now + timedelta(days=3, minutes=1), now), "in 3 days")
