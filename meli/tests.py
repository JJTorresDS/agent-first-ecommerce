import json
from datetime import timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from accounts.models import Membership, Organization
from meli.models import (
    ItemQuestion,
    MercadoLibreAccount,
    MercadoLibreNotification,
    SellerProfile,
)
from meli.tokens import encrypt_secret

User = get_user_model()


SAMPLE = {
    "_id": "5da8a1b24be30a49eb66c52a",
    "resource": "/orders/2195160686",
    "user_id": 468424240,
    "topic": "orders_v2",
    "application_id": 5503910054141466,
    "attempts": 1,
    "sent": "2019-10-30T16:19:20.129Z",
    "received": "2019-10-30T16:19:20.106Z",
}


WEBHOOK_SECRET = "hook-secret"
WEBHOOK_URL = f"/meli/webhooks/{WEBHOOK_SECRET}/"


def connect_seller(user_id="468424240"):
    organization = Organization.objects.create(name="Vimple")
    MercadoLibreAccount.objects.create(
        organization=organization,
        meli_user_id=user_id,
        access_token="cipher",
        expires_at=timezone.now() + timedelta(hours=1),
    )
    return organization


@override_settings(
    MELI_WEBHOOK_SECRET=WEBHOOK_SECRET,
    MELI_CLIENT_ID=str(SAMPLE["application_id"]),
)
class MercadoLibreWebhookTests(TestCase):
    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)

    def post_notification(self, body=None, url=WEBHOOK_URL):
        if body is None:
            body = json.dumps(SAMPLE)
        return self.client.post(url, data=body, content_type="application/json")

    @override_settings(MELI_WEBHOOK_SECRET="")
    def test_missing_secret_setting_is_not_found(self):
        response = self.post_notification()
        self.assertEqual(response.status_code, 404)
        self.assertEqual(MercadoLibreNotification.objects.count(), 0)

    def test_wrong_secret_is_not_found(self):
        response = self.post_notification(url="/meli/webhooks/wrong/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(MercadoLibreNotification.objects.count(), 0)
        missing = self.client.post(
            "/meli/webhooks/",
            data=json.dumps(SAMPLE),
            content_type="application/json",
        )
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(MercadoLibreNotification.objects.count(), 0)

    def test_known_seller_is_stored_on_their_organization(self):
        organization = connect_seller()
        response = self.post_notification()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok")
        row = MercadoLibreNotification.objects.get()
        self.assertEqual(row.notification_id, SAMPLE["_id"])
        self.assertEqual(row.topic, "orders_v2")
        self.assertEqual(row.resource, "/orders/2195160686")
        self.assertEqual(row.user_id, "468424240")
        self.assertEqual(row.organization, organization)
        self.assertEqual(ItemQuestion.objects.count(), 0)
        self.assertEqual(row.payload["topic"], "orders_v2")
        self.assertIsNotNone(row.sent_at)
        self.assertIsNotNone(row.received_at)
        self.assertEqual(
            row.buenos_aires_at.astimezone(ZoneInfo("America/Argentina/Buenos_Aires")).utcoffset(),
            timedelta(hours=-3),
        )

    def test_repeated_id_is_stored_once(self):
        connect_seller()
        body = json.dumps(SAMPLE)
        self.post_notification(body)
        second = self.post_notification(body)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(MercadoLibreNotification.objects.count(), 1)

    def test_unknown_seller_is_not_stored(self):
        response = self.post_notification()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok")
        self.assertEqual(MercadoLibreNotification.objects.count(), 0)

    def test_foreign_application_is_not_stored(self):
        connect_seller()
        body = json.dumps({**SAMPLE, "application_id": 1})
        response = self.post_notification(body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(MercadoLibreNotification.objects.count(), 0)

    def test_non_json_is_not_stored(self):
        connect_seller()
        response = self.client.post(
            WEBHOOK_URL,
            data="not-json",
            content_type="text/plain",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok")
        self.assertEqual(MercadoLibreNotification.objects.count(), 0)


class TokenResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


TOKEN = {
    "access_token": "access-1",
    "refresh_token": "refresh-1",
    "expires_in": 21600,
    "user_id": 468424240,
}


@override_settings(
    MELI_CLIENT_ID="app-id",
    MELI_CLIENT_SECRET="secret",
    MELI_AUTH_URL="https://auth.mercadolibre.com.ar/authorization",
)
class MercadoLibreConnectTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("ada", email="ada@example.com")
        self.organization = Organization.objects.create(name="Vimple")
        Membership.objects.create(
            user=self.owner,
            organization=self.organization,
            role=Membership.Role.OWNER,
        )
        self.client.force_login(self.owner)

    def test_non_owner_cannot_start_the_connection(self):
        operator = User.objects.create_user("bea", email="bea@example.com")
        Membership.objects.create(
            user=operator,
            organization=self.organization,
            role=Membership.Role.OPERATOR,
        )
        self.client.force_login(operator)
        response = self.client.get("/meli/connect/")
        self.assertEqual(response.status_code, 403)
        page = self.client.get("/meli/")
        self.assertContains(page, "Not connected")
        self.assertNotContains(page, 'href="/meli/connect/"')
        self.assertNotContains(page, "Create access token")

    def test_owner_is_sent_to_mercadolibre(self):
        response = self.client.get("/meli/connect/")
        self.assertEqual(response.status_code, 302)
        location = response["Location"]
        self.assertTrue(
            location.startswith("https://auth.mercadolibre.com.ar/authorization?")
        )
        self.assertIn("client_id=app-id", location)
        self.assertIn("state=", location)
        page = self.client.get("/meli/")
        self.assertContains(page, "Connect")
        self.assertContains(page, 'nav-label">Integrations')
        self.assertContains(page, "Mercado Libre")
        self.assertContains(page, "Settings")
        organization = self.client.get("/organization/")
        self.assertNotContains(organization, "Not connected")
        self.assertNotContains(organization, "Create access token")

    def _state(self):
        response = self.client.get("/meli/connect/")
        return parse_qs(urlparse(response["Location"]).query)["state"][0]

    def test_callback_connects_only_this_organization(self):
        state = self._state()
        with patch("meli.client.urlopen", return_value=TokenResponse(TOKEN)):
            response = self.client.get(
                "/meli/callback/",
                {"code": "auth-code", "state": state},
            )
        self.assertRedirects(response, "/meli/")
        account = MercadoLibreAccount.objects.get()
        self.assertEqual(account.organization, self.organization)
        self.assertEqual(account.meli_user_id, "468424240")
        self.assertNotEqual(account.access_token, "access-1")
        self.assertEqual(account.fresh_access_token(), "access-1")

        other_owner = User.objects.create_user("cara", email="cara@example.com")
        Organization.objects.create(name="Other")
        other = Organization.objects.get(name="Other")
        Membership.objects.create(
            user=other_owner,
            organization=other,
            role=Membership.Role.OWNER,
        )
        self.client.force_login(other_owner)
        other_state = self._state()
        with patch("meli.client.urlopen", return_value=TokenResponse(TOKEN)):
            taken = self.client.get(
                "/meli/callback/",
                {"code": "auth-code", "state": other_state},
            )
        self.assertRedirects(taken, "/meli/?meli=taken")
        self.assertEqual(MercadoLibreAccount.objects.count(), 1)
        self.assertEqual(
            MercadoLibreAccount.objects.get().organization,
            self.organization,
        )

    def test_valid_token_is_shown_without_calling_mercadolibre(self):
        MercadoLibreAccount.objects.create(
            organization=self.organization,
            meli_user_id="468424240",
            access_token=encrypt_secret("current-access"),
            refresh_token=encrypt_secret("refresh-1"),
            expires_at=timezone.now() + timedelta(hours=1),
        )
        with patch("meli.client.urlopen") as urlopen:
            response = self.client.post("/meli/token/")
        urlopen.assert_not_called()
        self.assertContains(response, "current-access")
        self.assertContains(response, "https://api.mercadolibre.com/users/me")
        self.assertContains(response, "Authorization: Bearer")

    def test_expired_token_is_refreshed(self):
        MercadoLibreAccount.objects.create(
            organization=self.organization,
            meli_user_id="468424240",
            access_token=encrypt_secret("old-access"),
            refresh_token=encrypt_secret("refresh-1"),
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        refreshed = {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 21600,
            "user_id": 468424240,
        }
        with patch("meli.client.urlopen", return_value=TokenResponse(refreshed)):
            response = self.client.post("/meli/token/")
        self.assertContains(response, "new-access")
        self.assertContains(response, "https://api.mercadolibre.com/users/me")
        self.assertNotContains(response, "old-access")
        self.assertEqual(
            MercadoLibreAccount.objects.get().fresh_access_token(),
            "new-access",
        )

    def test_non_owner_cannot_reveal_a_token(self):
        MercadoLibreAccount.objects.create(
            organization=self.organization,
            meli_user_id="468424240",
            access_token=encrypt_secret("current-access"),
            refresh_token=encrypt_secret("refresh-1"),
            expires_at=timezone.now() + timedelta(hours=1),
        )
        operator = User.objects.create_user("bea", email="bea@example.com")
        Membership.objects.create(
            user=operator,
            organization=self.organization,
            role=Membership.Role.OPERATOR,
        )
        self.client.force_login(operator)
        response = self.client.post("/meli/token/")
        self.assertEqual(response.status_code, 403)
        self.assertNotContains(response, "current-access", status_code=403)

    def test_callback_stores_the_seller_profile(self):
        state = self._state()
        with patch("meli.client.urlopen", side_effect=mercadolibre_responses(PROFILE)):
            response = self.client.get(
                "/meli/callback/",
                {"code": "auth-code", "state": state},
            )
            self.assertRedirects(response, "/meli/")
        profile = SellerProfile.objects.get()
        self.assertEqual(profile.organization, self.organization)
        self.assertIsNone(profile.deleted_at)
        self.assertEqual(profile.nickname, "SELLER")
        page = self.client.get("/meli/")
        self.assertContains(page, "SELLER")
        self.assertContains(page, "Ada Lovelace")
        self.assertContains(page, "ada@seller.test")
        self.assertContains(page, "MLA")

    def test_disconnect_hides_the_profile_until_reconnect(self):
        state = self._state()
        profiles = {"current": PROFILE}
        with patch(
            "meli.client.urlopen",
            side_effect=lambda request, timeout=15: mercadolibre_responses(
                profiles["current"]
            )(request, timeout),
        ):
            self.client.get("/meli/callback/", {"code": "auth-code", "state": state})
            self.client.post("/meli/disconnect/")
        profile = SellerProfile.objects.get()
        self.assertIsNotNone(profile.deleted_at)
        self.assertEqual(MercadoLibreAccount.objects.count(), 0)
        hidden = self.client.get("/meli/")
        self.assertNotContains(hidden, "SELLER")
        self.assertContains(hidden, "Not connected")

        profiles["current"] = {**PROFILE, "nickname": "NEWSELLER"}
        state = self._state()
        with patch(
            "meli.client.urlopen",
            side_effect=lambda request, timeout=15: mercadolibre_responses(
                profiles["current"]
            )(request, timeout),
        ):
            self.client.get("/meli/callback/", {"code": "auth-code", "state": state})
        profile.refresh_from_db()
        self.assertIsNone(profile.deleted_at)
        self.assertEqual(profile.nickname, "NEWSELLER")
        self.assertEqual(SellerProfile.objects.count(), 1)
        page = self.client.get("/meli/")
        self.assertContains(page, "NEWSELLER")
        self.assertNotContains(page, ">SELLER<")

    def test_failed_profile_call_keeps_the_account(self):
        state = self._state()
        with patch(
            "meli.client.urlopen",
            side_effect=mercadolibre_responses(URLError("down")),
        ):
            response = self.client.get(
                "/meli/callback/",
                {"code": "auth-code", "state": state},
                follow=True,
            )
        self.assertEqual(response.redirect_chain, [("/meli/?meli=profile", 302)])
        self.assertContains(response, "The seller profile could not be loaded.")
        self.assertEqual(MercadoLibreAccount.objects.count(), 1)
        self.assertEqual(SellerProfile.objects.count(), 0)


PROFILE = {
    "id": 468424240,
    "nickname": "SELLER",
    "first_name": "Ada",
    "last_name": "Lovelace",
    "email": "ada@seller.test",
    "site_id": "MLA",
    "country_id": "AR",
    "permalink": "https://perfil.mercadolibre.com.ar/SELLER",
}


def mercadolibre_responses(profile):
    def open_response(request, timeout=15):
        if request.full_url.endswith("/users/me"):
            if isinstance(profile, Exception):
                raise profile
            return TokenResponse(profile)
        return TokenResponse(TOKEN)

    return open_response


QUESTION = {
    "id": 13683341904,
    "text": "Tienen stock?",
    "status": "UNANSWERED",
    "item_id": "MLA1",
    "date_created": "2026-10-30T13:00:00.000-03:00",
    "from": {"id": 99},
    "answer": None,
}
ITEM = {
    "id": "MLA1",
    "title": "Perfume",
    "permalink": "https://articulo.mercadolibre.com.ar/MLA1",
}
ANSWERED = {
    **QUESTION,
    "status": "ANSWERED",
    "answer": {
        "text": "Si hay",
        "date_created": "2026-10-30T14:00:00.000-03:00",
    },
}


def question_api(request, timeout=15):
    url = request.full_url
    if url.endswith("/answers"):
        return TokenResponse({"text": "Si hay"})
    if "/items/" in url:
        return TokenResponse(ITEM)
    if "/questions/" in url:
        return TokenResponse(QUESTION)
    raise AssertionError(url)


@override_settings(
    MELI_WEBHOOK_SECRET=WEBHOOK_SECRET,
    MELI_CLIENT_ID=str(SAMPLE["application_id"]),
)
class ItemQuestionTests(TestCase):
    def setUp(self):
        self.organization = connect_seller()
        self.user = User.objects.create_user("ada", email="ada@example.com")
        Membership.objects.create(
            user=self.user,
            organization=self.organization,
            role=Membership.Role.OWNER,
        )
        self.client.force_login(self.user)
        account = MercadoLibreAccount.objects.get(organization=self.organization)
        account.access_token = encrypt_secret("access-1")
        account.refresh_token = encrypt_secret("refresh-1")
        account.save(update_fields=["access_token", "refresh_token", "updated_at"])

    def test_questions_notification_is_shown_in_the_chat(self):
        body = json.dumps(
            {
                **SAMPLE,
                "_id": "question-note",
                "topic": "questions",
                "resource": "/questions/13683341904",
            }
        )
        with patch("meli.client.urlopen", side_effect=question_api):
            response = self.client.post(
                WEBHOOK_URL,
                data=body,
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        question = ItemQuestion.objects.get()
        self.assertEqual(question.organization, self.organization)
        self.assertEqual(question.text, "Tienen stock?")
        self.assertEqual(question.item_title, "Perfume")
        page = self.client.get("/meli/questions/")
        self.assertContains(page, "Tienen stock?")
        self.assertContains(page, "Perfume")

    def test_reply_is_posted_to_mercadolibre(self):
        ItemQuestion.objects.create(
            organization=self.organization,
            question_id="13683341904",
            item_id="MLA1",
            item_title="Perfume",
            text="Tienen stock?",
            status="UNANSWERED",
        )
        calls = []

        def open_response(request, timeout=15):
            calls.append(request.full_url)
            if "/questions/" in request.full_url:
                return TokenResponse(ANSWERED)
            return question_api(request, timeout)

        with patch("meli.client.urlopen", side_effect=open_response):
            response = self.client.post(
                "/meli/questions/13683341904/reply/",
                {"text": "Si hay"},
            )
        self.assertTrue(any(url.endswith("/answers") for url in calls))
        self.assertRedirects(
            response,
            "/meli/questions/?question=13683341904",
        )
        self.assertEqual(ItemQuestion.objects.get().answer_text, "Si hay")
        page = self.client.get("/meli/questions/?question=13683341904")
        self.assertContains(page, "Si hay")
        self.assertNotContains(page, "Write a reply")

    def test_another_organization_does_not_see_the_question(self):
        ItemQuestion.objects.create(
            organization=self.organization,
            question_id="13683341904",
            text="Tienen stock?",
            status="UNANSWERED",
        )
        other_user = User.objects.create_user("bea", email="bea@example.com")
        other = Organization.objects.create(name="Other")
        Membership.objects.create(
            user=other_user,
            organization=other,
            role=Membership.Role.OWNER,
        )
        self.client.force_login(other_user)
        page = self.client.get("/meli/questions/")
        self.assertNotContains(page, "Tienen stock?")
        self.assertContains(page, "No questions yet.")

    def test_open_question_shows_a_count(self):
        ItemQuestion.objects.create(
            organization=self.organization,
            question_id="13683341904",
            item_title="Perfume",
            text="Tienen stock?",
            status="UNANSWERED",
        )
        page = self.client.get("/meli/questions/")
        self.assertContains(page, 'class="badge">1<')
        self.assertContains(page, 'class="open-dot"')
        organization = self.client.get("/organization/")
        self.assertContains(organization, 'class="alerts"')
        self.assertContains(organization, 'class="badge">1<')
        self.assertContains(organization, 'aria-label="Alerts, 1"')
        self.assertContains(organization, "You have 1 unanswered presales question")
        self.assertContains(organization, 'href="/meli/questions/"')

    def test_answered_question_has_no_count(self):
        ItemQuestion.objects.create(
            organization=self.organization,
            question_id="13683341904",
            text="Tienen stock?",
            status="ANSWERED",
            answer_text="Si hay",
        )
        page = self.client.get("/meli/questions/")
        self.assertNotContains(page, 'class="badge"')
        self.assertNotContains(page, 'class="open-dot"')
        organization = self.client.get("/organization/")
        self.assertContains(organization, 'class="alerts"')
        self.assertNotContains(organization, 'class="badge"')

    def test_another_organization_does_not_change_the_count(self):
        other = Organization.objects.create(name="Other")
        ItemQuestion.objects.create(
            organization=other,
            question_id="999",
            text="Only other",
            status="UNANSWERED",
        )
        page = self.client.get("/meli/questions/")
        self.assertNotContains(page, "Only other")
        self.assertNotContains(page, 'class="badge"')
        organization = self.client.get("/organization/")
        self.assertNotContains(organization, 'class="badge"')
        other_user = User.objects.create_user("bea", email="bea@example.com")
        Membership.objects.create(
            user=other_user,
            organization=other,
            role=Membership.Role.OWNER,
        )
        self.client.force_login(other_user)
        theirs = self.client.get("/organization/")
        self.assertEqual(theirs.context["alert_count"], 1)
        self.assertContains(theirs, "You have 1 unanswered presales question")
        self.assertContains(theirs, 'class="badge">1<')

    def test_user_without_an_organization_has_no_bell(self):
        ItemQuestion.objects.create(
            organization=self.organization,
            question_id="13683341904",
            text="Tienen stock?",
            status="UNANSWERED",
        )
        outsider = User.objects.create_user("cleo", email="cleo@example.com")
        self.client.force_login(outsider)
        page = self.client.get("/organization/setup/")
        self.assertEqual(page.context["alert_count"], 0)
        self.assertNotContains(page, 'class="alerts"')
        self.assertNotContains(page, 'class="badge"')

    def test_get_open_questions_loads_unanswered_from_mercadolibre(self):
        calls = []

        def open_response(request, timeout=15):
            calls.append(request.full_url)
            if "/questions/search" in request.full_url:
                self.assertIn("status=UNANSWERED", request.full_url)
                self.assertIn("seller_id=468424240", request.full_url)
                self.assertIn("limit=50", request.full_url)
                return TokenResponse({"questions": [QUESTION], "total": 1})
            return question_api(request, timeout)

        page = self.client.get("/meli/questions/")
        self.assertContains(page, "Get open questions")
        with patch("meli.client.urlopen", side_effect=open_response):
            response = self.client.post("/meli/questions/sync/")
        self.assertRedirects(response, "/meli/questions/?sync=ok")
        self.assertEqual(sum("/questions/search" in url for url in calls), 1)
        question = ItemQuestion.objects.get()
        self.assertEqual(question.organization, self.organization)
        self.assertEqual(question.text, "Tienen stock?")
        self.assertEqual(question.item_title, "Perfume")
        calls.clear()
        with patch("meli.client.urlopen", side_effect=open_response):
            shown = self.client.get("/meli/questions/?sync=ok")
        self.assertEqual(calls, [])
        self.assertContains(shown, "Tienen stock?")
        self.assertContains(shown, "Open questions loaded.")
        other_user = User.objects.create_user("bea", email="bea@example.com")
        other = Organization.objects.create(name="Other")
        Membership.objects.create(
            user=other_user,
            organization=other,
            role=Membership.Role.OWNER,
        )
        self.client.force_login(other_user)
        hidden = self.client.get("/meli/questions/")
        self.assertNotContains(hidden, "Tienen stock?")

    def test_sync_leaves_another_organizations_question_in_place(self):
        other = Organization.objects.create(name="Other")
        ItemQuestion.objects.create(
            organization=other,
            question_id="13683341904",
            text="Theirs",
            status="UNANSWERED",
        )

        def open_response(request, timeout=15):
            if "/questions/search" in request.full_url:
                return TokenResponse({"questions": [QUESTION]})
            return question_api(request, timeout)

        with patch("meli.client.urlopen", side_effect=open_response):
            self.client.post("/meli/questions/sync/")
        row = ItemQuestion.objects.get(question_id="13683341904")
        self.assertEqual(row.organization, other)
        self.assertEqual(row.text, "Theirs")
        page = self.client.get("/meli/questions/")
        self.assertNotContains(page, "Theirs")
        self.assertNotContains(page, "Tienen stock?")

    def test_sync_failure_stores_nothing(self):
        with patch("meli.client.urlopen", side_effect=URLError("down")):
            response = self.client.post("/meli/questions/sync/")
        self.assertRedirects(response, "/meli/questions/?sync=failed")
        self.assertEqual(ItemQuestion.objects.count(), 0)
        page = self.client.get("/meli/questions/?sync=failed")
        self.assertContains(page, "Open questions could not be loaded.")
