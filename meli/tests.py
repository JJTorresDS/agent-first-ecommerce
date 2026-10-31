import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from accounts.models import Membership, Organization
from inbox.models import Conversation, Message
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

    def test_admin_can_start_the_connection(self):
        admin = User.objects.create_user("bea", email="bea@example.com")
        Membership.objects.create(
            user=admin,
            organization=self.organization,
            role=Membership.Role.ADMIN,
        )
        self.client.force_login(admin)
        page = self.client.get("/meli/")
        self.assertContains(page, 'href="/meli/connect/"')
        response = self.client.get("/meli/connect/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response["Location"].startswith(
                "https://auth.mercadolibre.com.ar/authorization?"
            )
        )

    def test_contributor_cannot_start_the_connection(self):
        contributor = User.objects.create_user("bea", email="bea@example.com")
        Membership.objects.create(
            user=contributor,
            organization=self.organization,
            role=Membership.Role.CONTRIBUTOR,
        )
        self.client.force_login(contributor)
        response = self.client.get("/meli/connect/")
        self.assertEqual(response.status_code, 403)
        page = self.client.get("/meli/")
        self.assertContains(page, "Not connected")
        self.assertNotContains(page, 'href="/meli/connect/"')
        self.assertNotContains(page, "Create access token")

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
        conversation = Conversation.objects.get()
        self.assertEqual(conversation.organization, self.organization)
        self.assertEqual(conversation.channel, "meli-itemquestions")
        self.assertEqual(conversation.external_id, question.question_id)
        self.assertTrue(conversation.needs_reply)
        inbound = conversation.messages.get()
        self.assertEqual(inbound.direction, Message.Direction.INBOUND)
        self.assertEqual(inbound.body, "Tienen stock?")
        self.assertEqual(inbound.organization, self.organization)
        with patch("meli.client.urlopen", side_effect=question_api):
            self.client.post(WEBHOOK_URL, data=body, content_type="application/json")
        self.assertEqual(Conversation.objects.count(), 1)
        self.assertEqual(Message.objects.count(), 1)
        page = self.client.get("/meli/questions/", follow=True)
        self.assertContains(page, "Tienen stock?")
        self.assertContains(page, "Perfume")
        self.assertContains(page, "meli-itemquestions")

    def test_reply_is_posted_to_mercadolibre(self):
        ItemQuestion.objects.create(
            organization=self.organization,
            question_id="13683341904",
            item_id="MLA1",
            item_title="Perfume",
            text="Tienen stock?",
            status="UNANSWERED",
        )
        self.client.get("/inbox/")
        conversation = Conversation.objects.get(external_id="13683341904")
        calls = []

        def open_response(request, timeout=15):
            calls.append(request.full_url)
            if "/questions/" in request.full_url:
                return TokenResponse(ANSWERED)
            return question_api(request, timeout)

        with patch("meli.client.urlopen", side_effect=open_response):
            response = self.client.post(
                f"/inbox/{conversation.pk}/reply/",
                {"text": "Si hay"},
            )
        self.assertTrue(any(url.endswith("/answers") for url in calls))
        self.assertRedirects(
            response,
            f"/inbox/?channel=meli-itemquestions&conversation={conversation.pk}",
        )
        self.assertEqual(ItemQuestion.objects.get().answer_text, "Si hay")
        outbound = conversation.messages.get(direction=Message.Direction.OUTBOUND)
        self.assertEqual(outbound.body, "Si hay")
        self.assertEqual(outbound.organization, self.organization)
        self.assertFalse(Conversation.objects.get().needs_reply)
        page = self.client.get(f"/inbox/?conversation={conversation.pk}")
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
        page = self.client.get("/meli/questions/", follow=True)
        self.assertNotContains(page, "Tienen stock?")
        self.assertContains(page, "No messages yet.")

    def test_open_question_shows_a_count(self):
        ItemQuestion.objects.create(
            organization=self.organization,
            question_id="13683341904",
            item_title="Perfume",
            text="Tienen stock?",
            status="UNANSWERED",
        )
        page = self.client.get("/meli/questions/", follow=True)
        self.assertContains(page, 'class="badge">1<')
        self.assertContains(page, 'class="open-dot"')
        organization = self.client.get("/organization/")
        self.assertContains(organization, 'class="alerts"')
        self.assertContains(organization, 'class="badge">1<')
        self.assertContains(organization, 'aria-label="Alerts, 1"')
        self.assertContains(organization, "You have 1 unanswered presales question")
        self.assertContains(organization, 'href="/inbox/?channel=meli-itemquestions"')

    def test_answered_question_has_no_count(self):
        ItemQuestion.objects.create(
            organization=self.organization,
            question_id="13683341904",
            text="Tienen stock?",
            status="ANSWERED",
            answer_text="Si hay",
        )
        page = self.client.get("/meli/questions/", follow=True)
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
        page = self.client.get("/meli/questions/", follow=True)
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

        page = self.client.get("/meli/questions/", follow=True)
        self.assertContains(page, "Get open questions")
        with patch("meli.client.urlopen", side_effect=open_response):
            response = self.client.post("/meli/questions/sync/")
        self.assertRedirects(response, "/inbox/?channel=meli-itemquestions&sync=ok")
        self.assertEqual(sum("/questions/search" in url for url in calls), 1)
        question = ItemQuestion.objects.get()
        self.assertEqual(question.organization, self.organization)
        self.assertEqual(question.text, "Tienen stock?")
        self.assertEqual(question.item_title, "Perfume")
        conversation = Conversation.objects.get()
        self.assertEqual(conversation.organization, self.organization)
        self.assertEqual(conversation.channel, "meli-itemquestions")
        self.assertEqual(
            conversation.messages.get(direction=Message.Direction.INBOUND).body,
            "Tienen stock?",
        )
        calls.clear()
        with patch("meli.client.urlopen", side_effect=open_response):
            shown = self.client.get("/meli/questions/?sync=ok", follow=True)
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
        hidden = self.client.get("/meli/questions/", follow=True)
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
        self.assertFalse(Conversation.objects.filter(organization=self.organization).exists())
        self.assertFalse(Conversation.objects.filter(external_id="13683341904").exists())
        page = self.client.get("/meli/questions/", follow=True)
        self.assertNotContains(page, "Theirs")
        self.assertNotContains(page, "Tienen stock?")

    def test_sync_failure_stores_nothing(self):
        with patch("meli.client.urlopen", side_effect=URLError("down")):
            response = self.client.post("/meli/questions/sync/")
        self.assertRedirects(response, "/inbox/?channel=meli-itemquestions&sync=failed")
        self.assertEqual(ItemQuestion.objects.count(), 0)
        page = self.client.get("/meli/questions/?sync=failed", follow=True)
        self.assertContains(page, "Open questions could not be loaded.")

    def test_failed_reply_leaves_the_question_unanswered(self):
        ItemQuestion.objects.create(
            organization=self.organization,
            question_id="13683341904",
            text="Tienen stock?",
            status="UNANSWERED",
        )
        self.client.get("/inbox/")
        conversation = Conversation.objects.get()
        with patch("meli.client.urlopen", side_effect=URLError("down")):
            response = self.client.post(
                f"/inbox/{conversation.pk}/reply/",
                {"text": "Si hay"},
            )
        self.assertRedirects(
            response,
            f"/inbox/?channel=meli-itemquestions&conversation={conversation.pk}&reply=failed",
        )
        question = ItemQuestion.objects.get()
        self.assertEqual(question.status, "UNANSWERED")
        self.assertEqual(question.answer_text, "")
        self.assertFalse(conversation.messages.filter(direction=Message.Direction.OUTBOUND).exists())
        page = self.client.get(response.url)
        self.assertContains(page, "The answer could not be posted.")
        self.assertContains(page, "Write a reply")

    def test_another_organization_cannot_open_or_reply(self):
        ItemQuestion.objects.create(
            organization=self.organization,
            question_id="13683341904",
            text="Tienen stock?",
            status="UNANSWERED",
        )
        self.client.get("/inbox/")
        conversation = Conversation.objects.get()
        other_user = User.objects.create_user("bea", email="bea@example.com")
        other = Organization.objects.create(name="Other")
        Membership.objects.create(
            user=other_user,
            organization=other,
            role=Membership.Role.OWNER,
        )
        self.client.force_login(other_user)
        page = self.client.get(f"/inbox/?conversation={conversation.pk}")
        self.assertNotContains(page, "Tienen stock?")
        self.assertContains(page, "No messages yet.")
        reply = self.client.post(
            f"/inbox/{conversation.pk}/reply/",
            {"text": "No"},
        )
        self.assertEqual(reply.status_code, 404)
        self.assertEqual(ItemQuestion.objects.get().answer_text, "")

    def test_unknown_channel_hides_every_organization(self):
        ItemQuestion.objects.create(
            organization=self.organization,
            question_id="1",
            text="Mine",
            status="UNANSWERED",
        )
        other = Organization.objects.create(name="Other")
        ItemQuestion.objects.create(
            organization=other,
            question_id="2",
            text="Only other",
            status="UNANSWERED",
        )
        page = self.client.get("/inbox/?channel=not-a-channel")
        self.assertNotContains(page, "Mine")
        self.assertNotContains(page, "Only other")
        self.assertContains(page, "No messages yet.")

    def test_clearing_an_answer_removes_the_outbound_message(self):
        from meli.projection import project_question

        question = ItemQuestion.objects.create(
            organization=self.organization,
            question_id="13683341904",
            text="Tienen stock?",
            status="ANSWERED",
            answer_text="Si hay",
        )
        project_question(question)
        project_question(question)
        self.assertEqual(Message.objects.count(), 2)
        question.answer_text = ""
        question.status = "UNANSWERED"
        question.answered_at = None
        question.save()
        project_question(question)
        conversation = Conversation.objects.get()
        self.assertTrue(conversation.needs_reply)
        self.assertEqual(conversation.messages.count(), 1)
        self.assertEqual(conversation.messages.get().direction, Message.Direction.INBOUND)

    def test_migration_projects_an_existing_question(self):
        import importlib

        from django.apps import apps

        project_questions = importlib.import_module(
            "meli.migrations.0006_project_item_questions"
        ).project_questions
        ItemQuestion.objects.create(
            organization=self.organization,
            question_id="55",
            item_title="Perfume",
            item_permalink="https://articulo.mercadolibre.com.ar/MLA1",
            text="Tienen stock?",
            status="UNANSWERED",
        )
        project_questions(apps, None)
        conversation = Conversation.objects.get(external_id="55")
        self.assertEqual(conversation.organization, self.organization)
        self.assertEqual(conversation.channel, "meli-itemquestions")
        self.assertEqual(conversation.title, "Perfume")
        self.assertTrue(conversation.needs_reply)
        self.assertEqual(conversation.safe_link(), "https://articulo.mercadolibre.com.ar/MLA1")
        self.assertEqual(conversation.messages.get().body, "Tienen stock?")
        project_questions(apps, None)
        self.assertEqual(conversation.messages.count(), 1)


PACK = "2000000089077943"
TAKEN = "2000000089077944"
OLD = "2000000089077000"
UNREAD = "2000000089077999"


def sale_order(order_id, pack_id, created, first, last, title):
    return {
        "id": int(order_id),
        "pack_id": int(pack_id) if pack_id else None,
        "date_created": created,
        "buyer": {"id": 99, "first_name": first, "last_name": last},
        "order_items": [{"item": {"title": title}}],
    }


class _FakeRedis:
    def __init__(self):
        self.values = {}
        self.lists = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None):
        self.values[key] = value
        return True

    def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)
        return len(self.lists[key])

    def brpop(self, key, timeout=0):
        items = self.lists.get(key) or []
        if not items:
            return None
        return (key, items.pop())


def sale_thread(text, message_id="msg-1", sender=99, created=None):
    return {
        "conversation_status": {"status": "active"},
        "paging": {"total": 1, "limit": 50, "offset": 0},
        "messages": [
            {
                "id": message_id,
                "from": {"user_id": sender},
                "to": {"user_id": 468424240 if sender != 468424240 else 99},
                "text": text,
                "message_date": {"created": created or timezone.now().isoformat()},
            }
        ],
    }


@override_settings(
    MELI_WEBHOOK_SECRET=WEBHOOK_SECRET,
    MELI_CLIENT_ID=str(SAMPLE["application_id"]),
)
class PostSaleTests(TestCase):
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

    def test_messages_webhook_creates_the_chat_and_the_bell(self):
        created = timezone.now().isoformat()
        message = {
            "id": "msg-1",
            "from": {"user_id": 99},
            "to": {"user_id": 468424240},
            "text": "Necesito medidas?",
            "message_resources": [{"id": PACK, "name": "packs"}],
            "message_date": {"created": created},
        }
        order = sale_order(PACK, PACK, created, "Willian", "Guido", "Remera")

        def open_response(request, timeout=15):
            url = request.full_url
            if url.endswith(f"/messages/{message['id']}"):
                return TokenResponse(message)
            if "/messages/packs/" in url:
                self.assertIn("mark_as_read=false", url)
                self.assertIn("tag=post_sale", url)
                return TokenResponse(sale_thread("Necesito medidas?", created=created))
            if f"/orders/{PACK}" in url:
                return TokenResponse(order)
            raise AssertionError(url)

        body = json.dumps(
            {
                **SAMPLE,
                "_id": "post-sale-note",
                "topic": "messages",
                "resource": "/messages/msg-1",
            }
        )
        with patch("meli.client.urlopen", side_effect=open_response):
            response = self.client.post(
                WEBHOOK_URL,
                data=body,
                content_type="application/json",
            )
            self.client.post(WEBHOOK_URL, data=body, content_type="application/json")
        self.assertEqual(response.status_code, 200)
        conversation = Conversation.objects.get()
        self.assertEqual(conversation.organization, self.organization)
        self.assertEqual(conversation.channel, "meli-postsales")
        self.assertEqual(conversation.external_id, PACK)
        self.assertEqual(conversation.title, "Willian Guido · Remera")
        self.assertTrue(conversation.needs_reply)
        inbound = conversation.messages.get()
        self.assertEqual(inbound.direction, Message.Direction.INBOUND)
        self.assertEqual(inbound.body, "Necesito medidas?")
        self.assertEqual(inbound.external_id, "msg-1")
        self.assertEqual(Message.objects.count(), 1)
        page = self.client.get("/organization/")
        self.assertContains(page, "You have 1 unanswered post-sale message")
        self.assertContains(page, 'href="/inbox/?channel=meli-postsales"')
        self.assertNotContains(page, "post-sale messages")
        other_user = User.objects.create_user("bea", email="bea@example.com")
        other = Organization.objects.create(name="Other")
        Membership.objects.create(
            user=other_user,
            organization=other,
            role=Membership.Role.OWNER,
        )
        self.client.force_login(other_user)
        hidden = self.client.get("/inbox/")
        self.assertNotContains(hidden, "Necesito medidas?")
        self.assertContains(hidden, "No messages yet.")
        reply = self.client.post(
            f"/inbox/{conversation.pk}/reply/",
            {"text": "Mide 67x60cm"},
        )
        self.assertEqual(reply.status_code, 404)
        self.assertFalse(
            conversation.messages.filter(direction=Message.Direction.OUTBOUND).exists()
        )

    def test_sync_loads_recent_orders_and_unread_packs(self):
        now = timezone.now()
        recent = sale_order(PACK, PACK, now.isoformat(), "Willian", "Guido", "Remera")
        old = sale_order(
            OLD,
            None,
            (now - timedelta(days=30)).isoformat(),
            "Old",
            "Buyer",
            "Hidden",
        )
        taken = sale_order(TAKEN, TAKEN, now.isoformat(), "Other", "Buyer", "Taken")
        other = Organization.objects.create(name="Other")
        Conversation.objects.create(
            organization=other,
            channel="meli-postsales",
            external_id=TAKEN,
            title="Theirs",
            needs_reply=True,
            last_message_at=now,
        )
        calls = []

        def open_response(request, timeout=15):
            url = request.full_url
            calls.append(url)
            if "/orders/search" in url:
                self.assertIn("seller=468424240", url)
                query = parse_qs(urlparse(url).query)
                start = datetime.fromisoformat(
                    query["order.date_created.from"][0].replace("Z", "+00:00")
                )
                self.assertLess(
                    abs(now - timedelta(days=7) - start),
                    timedelta(minutes=5),
                )
                return TokenResponse(
                    {
                        "results": [recent, old, taken],
                        "paging": {"total": 3, "limit": 50, "offset": 0},
                    }
                )
            if "/messages/unread" in url:
                self.assertIn("tag=post_sale", url)
                return TokenResponse(
                    {"results": [{"resource": f"/packs/{UNREAD}", "count": 1}]}
                )
            if f"/messages/packs/{PACK}/" in url:
                self.assertIn("mark_as_read=false", url)
                return TokenResponse(sale_thread("Necesito medidas?"))
            if f"/messages/packs/{UNREAD}/" in url:
                self.assertIn("mark_as_read=false", url)
                return TokenResponse(sale_thread("Mensaje viejo", message_id="msg-old"))
            if f"/orders/{UNREAD}" in url:
                return TokenResponse(
                    sale_order(UNREAD, UNREAD, now.isoformat(), "Ana", "Paz", "Pantalon")
                )
            raise AssertionError(url)

        page = self.client.get("/inbox/")
        self.assertContains(page, "Get recent post-sale messages")
        with patch("meli.client.urlopen", side_effect=open_response):
            response = self.client.post("/meli/postsale/sync/")
        self.assertRedirects(
            response,
            "/inbox/?channel=meli-postsales&sync=ok",
        )
        self.assertFalse(Conversation.objects.filter(external_id=OLD).exists())
        kept = Conversation.objects.get(external_id=TAKEN)
        self.assertEqual(kept.organization, other)
        self.assertEqual(kept.title, "Theirs")
        loaded = Conversation.objects.get(external_id=PACK)
        self.assertEqual(loaded.organization, self.organization)
        self.assertEqual(loaded.title, "Willian Guido · Remera")
        self.assertEqual(loaded.messages.get().body, "Necesito medidas?")
        unread = Conversation.objects.get(external_id=UNREAD)
        self.assertEqual(unread.messages.get().body, "Mensaje viejo")
        self.assertFalse(any(TAKEN in url or OLD in url for url in calls if "/messages/packs/" in url))
        shown = self.client.get("/inbox/?channel=meli-postsales&sync=ok")
        self.assertContains(shown, "Post-sale messages loaded.")
        self.assertContains(shown, "Necesito medidas?")
        self.assertNotContains(shown, "Theirs")
        self.client.force_login(
            User.objects.create_user("bea", email="bea@example.com")
        )
        Membership.objects.create(
            user=User.objects.get(username="bea"),
            organization=other,
            role=Membership.Role.OWNER,
        )
        hidden = self.client.get("/inbox/")
        self.assertContains(hidden, "Theirs")
        self.assertNotContains(hidden, "Necesito medidas?")

    @override_settings(REDIS_URL="redis://postsale-test")
    def test_sync_is_queued_until_the_worker_runs(self):
        from meli.jobs import QUEUE, run_next_postsale_job, status_key

        fake = _FakeRedis()
        now = timezone.now()

        def open_response(request, timeout=15):
            url = request.full_url
            if "/orders/search" in url:
                return TokenResponse(
                    {
                        "results": [
                            sale_order(
                                PACK,
                                PACK,
                                now.isoformat(),
                                "Willian",
                                "Guido",
                                "Remera",
                            )
                        ],
                        "paging": {"total": 1, "limit": 50, "offset": 0},
                    }
                )
            if "/messages/unread" in url:
                return TokenResponse({"results": []})
            if f"/messages/packs/{PACK}/" in url:
                return TokenResponse(sale_thread("Necesito medidas?"))
            raise AssertionError(url)

        with (
            patch("meli.jobs._client", return_value=fake),
            patch("meli.client.urlopen", side_effect=open_response) as opened,
        ):
            response = self.client.post("/meli/postsale/sync/")
            self.assertRedirects(
                response,
                "/inbox/?channel=meli-postsales&sync=queued",
            )
            self.assertEqual(opened.call_count, 0)
            self.assertEqual(fake.lists[QUEUE], [str(self.organization.pk)])
            again = self.client.post("/meli/postsale/sync/")
            self.assertRedirects(
                again,
                "/inbox/?channel=meli-postsales&sync=queued",
            )
            self.assertEqual(fake.lists[QUEUE], [str(self.organization.pk)])
            waiting = self.client.get("/inbox/?channel=meli-postsales")
            self.assertContains(waiting, "Post-sale messages are loading.")
            self.assertContains(waiting, 'hx-trigger="every 3s"')
            self.assertFalse(Conversation.objects.filter(external_id=PACK).exists())
            self.assertTrue(run_next_postsale_job(block_seconds=0))
            loaded = self.client.get("/inbox/?channel=meli-postsales")
        self.assertEqual(fake.values[status_key(self.organization.pk)], "ok")
        self.assertContains(loaded, "Post-sale messages loaded.")
        self.assertNotContains(loaded, "Post-sale messages are loading.")
        self.assertNotContains(loaded, 'hx-trigger="every 3s"')
        self.assertEqual(
            Conversation.objects.get(external_id=PACK).messages.get().body,
            "Necesito medidas?",
        )

    def test_reply_posts_to_the_pack_and_clears_the_dot(self):
        now = timezone.now()
        conversation = Conversation.objects.create(
            organization=self.organization,
            channel="meli-postsales",
            external_id=PACK,
            title="Willian Guido · Remera",
            needs_reply=True,
            last_message_at=now,
        )
        Message.objects.create(
            organization=self.organization,
            conversation=conversation,
            direction=Message.Direction.INBOUND,
            body="Necesito medidas?",
            external_id="msg-1",
            sent_at=now,
        )
        after = {
            "conversation_status": {"status": "active"},
            "paging": {"total": 2, "limit": 50, "offset": 0},
            "messages": [
                sale_thread("Necesito medidas?", created=now.isoformat())["messages"][0],
                {
                    "id": "msg-2",
                    "from": {"user_id": 468424240},
                    "to": {"user_id": 99},
                    "text": "Mide 67x60cm",
                    "message_date": {"created": timezone.now().isoformat()},
                },
            ],
        }
        calls = []

        def open_response(request, timeout=15):
            calls.append(request.full_url)
            if getattr(request, "data", None):
                self.assertIn("/messages/packs/", request.full_url)
                self.assertIn("tag=post_sale", request.full_url)
                return TokenResponse({"id": "msg-2"})
            if "/messages/packs/" in request.full_url:
                if sum("/messages/packs/" in url for url in calls) > 1:
                    return TokenResponse(after)
                return TokenResponse(sale_thread("Necesito medidas?", created=now.isoformat()))
            if "/orders/" in request.full_url:
                return TokenResponse(
                    sale_order(PACK, PACK, now.isoformat(), "Willian", "Guido", "Remera")
                )
            raise AssertionError(request.full_url)

        with patch("meli.client.urlopen", side_effect=open_response):
            response = self.client.post(
                f"/inbox/{conversation.pk}/reply/",
                {"text": "Mide 67x60cm"},
            )
        self.assertTrue(any("/messages/packs/" in url and "tag=post_sale" in url for url in calls))
        self.assertRedirects(
            response,
            f"/inbox/?channel=meli-postsales&conversation={conversation.pk}",
        )
        conversation.refresh_from_db()
        self.assertFalse(conversation.needs_reply)
        outbound = conversation.messages.get(direction=Message.Direction.OUTBOUND)
        self.assertEqual(outbound.body, "Mide 67x60cm")
        page = self.client.get(f"/inbox/?conversation={conversation.pk}")
        self.assertContains(page, "Mide 67x60cm")
        self.assertNotContains(page, "Write a reply")

    def test_failed_reply_leaves_the_thread_unanswered(self):
        now = timezone.now()
        conversation = Conversation.objects.create(
            organization=self.organization,
            channel="meli-postsales",
            external_id=PACK,
            title="Willian Guido",
            needs_reply=True,
            last_message_at=now,
        )

        def open_response(request, timeout=15):
            if getattr(request, "data", None):
                raise URLError("down")
            if "/messages/packs/" in request.full_url:
                return TokenResponse(sale_thread("Necesito medidas?", created=now.isoformat()))
            raise AssertionError(request.full_url)

        with patch("meli.client.urlopen", side_effect=open_response):
            response = self.client.post(
                f"/inbox/{conversation.pk}/reply/",
                {"text": "Mide 67x60cm"},
            )
        self.assertRedirects(
            response,
            f"/inbox/?channel=meli-postsales&conversation={conversation.pk}&reply=failed",
        )
        conversation.refresh_from_db()
        self.assertTrue(conversation.needs_reply)
        self.assertFalse(
            conversation.messages.filter(direction=Message.Direction.OUTBOUND).exists()
        )
        page = self.client.get(response.url)
        self.assertContains(page, "The answer could not be posted.")
        self.assertContains(page, "Write a reply")

    def test_reply_over_350_characters_is_rejected(self):
        conversation = Conversation.objects.create(
            organization=self.organization,
            channel="meli-postsales",
            external_id=PACK,
            title="Willian Guido",
            needs_reply=True,
            last_message_at=timezone.now(),
        )
        with patch("meli.client.urlopen", side_effect=AssertionError("network")):
            response = self.client.post(
                f"/inbox/{conversation.pk}/reply/",
                {"text": "a" * 351},
            )
        self.assertRedirects(
            response,
            f"/inbox/?channel=meli-postsales&conversation={conversation.pk}&reply=failed",
        )
        conversation.refresh_from_db()
        self.assertTrue(conversation.needs_reply)

    def test_unanswered_comes_first_and_old_answered_is_hidden(self):
        now = timezone.now()
        Conversation.objects.create(
            organization=self.organization,
            channel="meli-postsales",
            external_id="old",
            title="Old sale",
            needs_reply=False,
            last_message_at=now - timedelta(days=8),
        )
        Conversation.objects.create(
            organization=self.organization,
            channel="meli-postsales",
            external_id="answered",
            title="Answered sale",
            needs_reply=False,
            last_message_at=now - timedelta(hours=1),
        )
        Conversation.objects.create(
            organization=self.organization,
            channel="meli-postsales",
            external_id="waiting",
            title="Waiting sale",
            needs_reply=True,
            last_message_at=now - timedelta(days=2),
        )
        page = self.client.get("/inbox/")
        content = page.content.decode()
        self.assertLess(content.index("Waiting sale"), content.index("Answered sale"))
        self.assertNotContains(page, "Old sale")
        self.assertContains(page, "Show older")
        older = self.client.get("/inbox/?since=all")
        self.assertContains(older, "Old sale")
        self.assertContains(older, "Waiting sale")
        self.assertContains(older, "Last 7 days")

    def test_attachment_only_message_is_labeled(self):
        from meli.postsale import project_pack

        project_pack(
            self.organization,
            "468424240",
            PACK,
            {
                "conversation_status": {"status": "active"},
                "messages": [
                    {
                        "id": "file-1",
                        "from": {"user_id": 99},
                        "text": "  ",
                        "message_attachments": ["photo.jpg"],
                        "message_date": {"created": timezone.now().isoformat()},
                    }
                ],
            },
            sale_order(PACK, PACK, timezone.now().isoformat(), "Willian", "Guido", "Remera"),
        )
        self.assertEqual(Conversation.objects.get().messages.get().body, "Attachment")
        self.assertTrue(Conversation.objects.get().needs_reply)
