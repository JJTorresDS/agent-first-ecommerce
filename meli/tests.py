import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch
from io import BytesIO
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
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
from meli.projection import project_question
from meli.tokens import decrypt_secret, encrypt_secret

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

    def test_callback_connects_the_same_seller_to_a_second_organization(self):
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
        second_token = {**TOKEN, "access_token": "access-2", "refresh_token": "refresh-2"}

        with patch("meli.client.urlopen", return_value=TokenResponse(second_token)):
            shared = self.client.get(
                "/meli/callback/",
                {"code": "auth-code", "state": other_state},
            )
        self.assertRedirects(shared, "/meli/")
        self.assertEqual(MercadoLibreAccount.objects.count(), 2)
        mine = MercadoLibreAccount.objects.get(organization=self.organization)
        theirs = MercadoLibreAccount.objects.get(organization=other)
        self.assertEqual(theirs.meli_user_id, "468424240")
        self.assertEqual(mine.fresh_access_token(), "access-2")
        self.assertEqual(decrypt_secret(mine.refresh_token), "refresh-2")
        self.assertEqual(theirs.fresh_access_token(), "access-2")
        with self.assertRaises(IntegrityError), transaction.atomic():
            MercadoLibreAccount.objects.create(
                organization=other,
                meli_user_id="999",
                access_token="x",
                expires_at=timezone.now(),
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
        project_question(ItemQuestion.objects.get())
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
            f"/inbox/?conversation={conversation.pk}",
        )
        self.assertEqual(ItemQuestion.objects.get().answer_text, "Si hay")
        outbound = conversation.messages.get(direction=Message.Direction.OUTBOUND)
        self.assertEqual(outbound.body, "Si hay")
        self.assertEqual(outbound.organization, self.organization)
        self.assertFalse(Conversation.objects.get().needs_reply)
        page = self.client.get(f"/inbox/?conversation={conversation.pk}")
        self.assertContains(page, "Si hay")
        self.assertNotContains(page, "Write a reply")

    def test_reply_from_the_live_origin_is_not_a_csrf_failure(self):
        ItemQuestion.objects.create(
            organization=self.organization,
            question_id="13683341904",
            item_id="MLA1",
            item_title="Perfume",
            text="Tienen stock?",
            status="UNANSWERED",
        )
        project_question(ItemQuestion.objects.get())
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        page = client.get("/inbox/")
        conversation = Conversation.objects.get(external_id="13683341904")
        token = (
            page.content.decode()
            .split('name="csrfmiddlewaretoken" value="', 1)[1]
            .split('"', 1)[0]
        )
        with patch("meli.client.urlopen", side_effect=question_api):
            response = client.post(
                f"/inbox/{conversation.pk}/reply/",
                {"text": "Si hay", "csrfmiddlewaretoken": token},
                HTTP_ORIGIN="https://vimple-backend.onrender.com",
            )
        self.assertNotEqual(response.status_code, 403)
        self.assertRedirects(
            response,
            f"/inbox/?conversation={conversation.pk}",
        )

    def test_opening_the_inbox_does_not_call_mercadolibre(self):
        MercadoLibreNotification.objects.create(
            organization=self.organization,
            topic="questions",
            resource="/questions/13683341904",
            user_id="468424240",
            payload={
                "topic": "questions",
                "resource": "/questions/13683341904",
            },
        )
        with patch("meli.client.urlopen", return_value=TokenResponse(QUESTION)) as opened:
            page = self.client.get("/inbox/")
        self.assertEqual(opened.call_count, 0)
        self.assertEqual(ItemQuestion.objects.count(), 0)
        self.assertContains(page, "No messages yet.")

    def test_refresh_stores_an_answer_made_on_mercadolibre(self):
        from meli.jobs import refresh_unanswered

        project_question(
            ItemQuestion.objects.create(
                organization=self.organization,
                question_id="13683341904",
                item_id="MLA1",
                item_title="Perfume",
                text="Tienen stock?",
                status="UNANSWERED",
            )
        )
        other = Organization.objects.create(name="Other")
        project_question(
            ItemQuestion.objects.create(
                organization=other,
                question_id="999",
                item_id="MLA9",
                text="Only other",
                status="UNANSWERED",
            )
        )

        def open_response(request, timeout=15):
            url = request.full_url
            self.assertNotIn("/questions/999", url)
            if "/questions/" in url:
                return TokenResponse(ANSWERED)
            if "/items/" in url:
                return TokenResponse(ITEM)
            raise AssertionError(url)

        with patch("meli.client.urlopen", side_effect=open_response) as opened:
            page = self.client.get("/inbox/")
            self.assertEqual(opened.call_count, 0)
            self.assertContains(page, 'class="open-dot"')
            refresh_unanswered(self.organization.pk)
            self.assertGreater(opened.call_count, 0)
        question = ItemQuestion.objects.get(question_id="13683341904")
        self.assertEqual(question.answer_text, "Si hay")
        self.assertEqual(question.status, "ANSWERED")
        conversation = Conversation.objects.get(
            organization=self.organization,
            external_id="13683341904",
        )
        self.assertFalse(conversation.needs_reply)
        outbound = conversation.messages.get(direction=Message.Direction.OUTBOUND)
        self.assertEqual(outbound.body, "Si hay")
        self.assertEqual(
            ItemQuestion.objects.get(question_id="999").status,
            "UNANSWERED",
        )
        shown = self.client.get(f"/inbox/?conversation={conversation.pk}")
        self.assertContains(shown, "Si hay")
        self.assertNotContains(shown, 'class="open-dot"')

    def test_bell_counts_needs_reply_without_rewriting_questions(self):
        project_question(
            ItemQuestion.objects.create(
                organization=self.organization,
                question_id="1",
                text="Mine",
                status="UNANSWERED",
            )
        )
        ItemQuestion.objects.create(
            organization=self.organization,
            question_id="2",
            text="Not projected",
            status="UNANSWERED",
        )
        other = Organization.objects.create(name="Other")
        project_question(
            ItemQuestion.objects.create(
                organization=other,
                question_id="3",
                text="Theirs",
                status="UNANSWERED",
            )
        )
        with patch("inbox.channels.reconcile", side_effect=AssertionError("reconcile")):
            page = self.client.get("/organization/")
        self.assertEqual(page.context["alert_count"], 1)
        self.assertContains(page, "You have 1 unanswered presales question")

    @override_settings(REDIS_URL="redis://reply-test")
    def test_reply_is_queued_until_the_worker_runs(self):
        import json

        from meli.jobs import (
            REFRESH_QUEUE,
            REPLY_LANE,
            REPLY_QUEUE,
            WORKER_QUEUES,
            refresh_status_key,
            reply_status_key,
            run_next_job,
            run_reply_job,
        )

        project_question(
            ItemQuestion.objects.create(
                organization=self.organization,
                question_id="13683341904",
                item_id="MLA1",
                item_title="Perfume",
                text="Tienen stock?",
                status="UNANSWERED",
            )
        )
        conversation = Conversation.objects.get(external_id="13683341904")
        fake = _FakeRedis()

        def open_response(request, timeout=15):
            if "/questions/" in request.full_url and not request.full_url.endswith("/answers"):
                return TokenResponse(ANSWERED)
            return question_api(request, timeout)

        with (
            patch("meli.jobs._client", return_value=fake),
            patch("meli.client.urlopen", side_effect=open_response) as opened,
        ):
            response = self.client.post(
                f"/inbox/{conversation.pk}/reply/",
                {"text": "Si hay"},
                HTTP_HX_REQUEST="true",
                HTTP_HX_TARGET="thread",
            )
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'id="thread"')
            self.assertContains(response, "Si hay")
            self.assertContains(response, 'hx-trigger="every 1s"')
            self.assertContains(response, 'hx-swap-oob="outerHTML"')
            self.assertNotContains(response, 'id="chat-list"')
            self.assertContains(response, 'data-sending="true"')
            self.assertContains(response, "Sending…")
            self.assertContains(response, "poll=sending")
            self.assertEqual(opened.call_count, 0)
            queued = json.loads(fake.lists[REPLY_QUEUE][0])
            self.assertEqual(queued["organization_id"], str(self.organization.pk))
            self.assertEqual(queued["conversation_id"], str(conversation.pk))
            self.assertEqual(ItemQuestion.objects.get().answer_text, "")
            again = self.client.post(
                f"/inbox/{conversation.pk}/reply/",
                {"text": "Otra"},
                HTTP_HX_REQUEST="true",
                HTTP_HX_TARGET="thread",
            )
            self.assertEqual(again.status_code, 200)
            self.assertEqual(len(fake.lists[REPLY_QUEUE]), 1)
            self.assertEqual(opened.call_count, 0)
            other = Organization.objects.create(name="Other")
            run_reply_job(other.pk, conversation.pk)
            self.assertEqual(opened.call_count, 0)
            self.assertEqual(ItemQuestion.objects.get().answer_text, "")
            # Replies have their own lane: it takes the reply and never the refresh,
            # and the general lane never takes a reply.
            fake.lpush(REFRESH_QUEUE, str(self.organization.pk))
            self.assertNotIn(REPLY_QUEUE, WORKER_QUEUES)
            with self.assertLogs("meli", "INFO") as logs:
                self.assertTrue(run_next_job(queues=REPLY_LANE))
            self.assertIn("sent in", "\n".join(logs.output))
            self.assertEqual(fake.lists[REFRESH_QUEUE], [str(self.organization.pk)])
            self.assertFalse(run_next_job(queues=REPLY_LANE))
            calls = opened.call_count
            # A refresh queued before the last one finished is skipped.
            fake.set(refresh_status_key(self.organization.pk), "ok")
            self.assertTrue(run_next_job())
            self.assertEqual(fake.lists[REFRESH_QUEUE], [])
            self.assertEqual(opened.call_count, calls)
            self.assertFalse(run_next_job())
        self.assertEqual(fake.values[reply_status_key(conversation.pk)], "ok")
        self.assertEqual(ItemQuestion.objects.get().answer_text, "Si hay")
        self.assertFalse(
            conversation.messages.filter(external_id="pending").exists()
        )
        self.assertEqual(
            conversation.messages.get(direction=Message.Direction.OUTBOUND).body,
            "Si hay",
        )

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
        self.assertContains(organization, 'href="/inbox/"')

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
        project_question(
            ItemQuestion.objects.create(
                organization=other,
                question_id="999",
                text="Only other",
                status="UNANSWERED",
            )
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
        self.assertRedirects(response, "/inbox/?sync=ok")
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

    def test_sync_copies_a_shared_question_without_touching_the_other_organization(self):
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
        theirs = ItemQuestion.objects.get(organization=other, question_id="13683341904")
        self.assertEqual(theirs.text, "Theirs")
        mine = ItemQuestion.objects.get(
            organization=self.organization,
            question_id="13683341904",
        )
        self.assertEqual(mine.text, "Tienen stock?")
        self.assertTrue(
            Conversation.objects.filter(
                organization=self.organization,
                external_id="13683341904",
            ).exists()
        )
        self.assertFalse(Conversation.objects.filter(organization=other).exists())
        page = self.client.get("/meli/questions/", follow=True)
        self.assertNotContains(page, "Theirs")
        self.assertContains(page, "Tienen stock?")

    def test_sync_failure_stores_nothing(self):
        with patch("meli.client.urlopen", side_effect=URLError("down")):
            response = self.client.post("/meli/questions/sync/")
        self.assertRedirects(response, "/inbox/?sync=failed")
        self.assertEqual(ItemQuestion.objects.count(), 0)
        page = self.client.get("/meli/questions/?sync=failed", follow=True)
        self.assertContains(page, "Open questions could not be loaded.")

    def test_an_answer_is_one_call_then_a_background_reread(self):
        ItemQuestion.objects.create(
            organization=self.organization,
            question_id="13683341904",
            text="Tienen stock?",
            status="UNANSWERED",
        )
        project_question(ItemQuestion.objects.get())
        conversation = Conversation.objects.get()
        calls = []

        def open_response(request, timeout=15):
            calls.append(request.full_url)
            if request.full_url.endswith("/answers"):
                return TokenResponse({"text": "Si hay"})
            if "/questions/" in request.full_url:
                return TokenResponse(ANSWERED)
            return question_api(request, timeout)

        with patch("meli.client.urlopen", side_effect=open_response):
            self.client.post(f"/inbox/{conversation.pk}/reply/", {"text": "Si hay"})
        self.assertTrue(calls[0].endswith("/answers"))
        conversation.refresh_from_db()
        self.assertFalse(conversation.needs_reply)
        self.assertEqual(ItemQuestion.objects.get().answer_text, "Si hay")
        # The re-read after it (inline under tests) keeps the answer.
        self.assertTrue(any("/questions/13683341904" in url for url in calls[1:]))

    def test_failed_reply_leaves_the_question_unanswered(self):
        ItemQuestion.objects.create(
            organization=self.organization,
            question_id="13683341904",
            text="Tienen stock?",
            status="UNANSWERED",
        )
        project_question(ItemQuestion.objects.get())
        conversation = Conversation.objects.get()
        with patch("meli.client.urlopen", side_effect=URLError("down")):
            response = self.client.post(
                f"/inbox/{conversation.pk}/reply/",
                {"text": "Si hay"},
            )
        self.assertRedirects(
            response,
            f"/inbox/?conversation={conversation.pk}&reply=failed",
        )
        question = ItemQuestion.objects.get()
        self.assertEqual(question.status, "UNANSWERED")
        self.assertEqual(question.answer_text, "")
        self.assertFalse(conversation.messages.filter(direction=Message.Direction.OUTBOUND).exists())
        page = self.client.get(response.url)
        self.assertContains(page, "Mercado Libre could not be reached. Try again.")
        self.assertContains(page, '<blockquote class="unsent">Si hay</blockquote>')
        self.assertContains(page, "Write a reply")

    def test_mercadolibre_refusal_of_an_answer_is_shown(self):
        ItemQuestion.objects.create(
            organization=self.organization,
            question_id="13683341904",
            text="Tienen stock?",
            status="UNANSWERED",
        )
        project_question(ItemQuestion.objects.get())
        conversation = Conversation.objects.get()

        def open_response(request, timeout=15):
            if request.full_url.endswith("/answers"):
                raise refused(400, "", message="Question already answered")
            return question_api(request, timeout)

        with patch("meli.client.urlopen", side_effect=open_response):
            self.client.post(f"/inbox/{conversation.pk}/reply/", {"text": "Si hay"})
        self.assertEqual(ItemQuestion.objects.get().answer_text, "")
        page = self.client.get(f"/inbox/?conversation={conversation.pk}")
        self.assertContains(
            page,
            "Mercado Libre did not accept the message: Question already answered",
        )

    def test_the_composer_shows_the_send_shortcut(self):
        ItemQuestion.objects.create(
            organization=self.organization,
            question_id="13683341904",
            text="Tienen stock?",
            status="UNANSWERED",
        )
        project_question(ItemQuestion.objects.get())
        conversation = Conversation.objects.get()
        page = self.client.get(f"/inbox/?conversation={conversation.pk}")
        self.assertContains(page, 'aria-keyshortcuts="Meta+Enter Control+Enter"')
        self.assertContains(page, '<kbd class="key-mac">⌘</kbd><kbd class="key-other">Ctrl</kbd>')

    def test_the_thread_marks_its_newest_message(self):
        ItemQuestion.objects.create(
            organization=self.organization,
            question_id="13683341904",
            text="Tienen stock?",
            status="UNANSWERED",
        )
        project_question(ItemQuestion.objects.get())
        conversation = Conversation.objects.get()
        newest = conversation.messages.order_by("sent_at", "created_at").last()
        thread = self.client.get(
            f"/inbox/?conversation={conversation.pk}",
            headers={"HX-Request": "true", "HX-Target": "thread"},
        )
        self.assertContains(
            thread,
            f'id="bubbles" data-last="{newest.external_id}" data-count="1"',
        )
        empty = Conversation.objects.create(
            organization=self.organization,
            channel="meli-postsales",
            external_id="9",
            title="Empty",
        )
        page = self.client.get(f"/inbox/?conversation={empty.pk}")
        self.assertContains(page, 'id="bubbles" data-last="" data-count="0"')

    def test_an_answered_question_has_no_composer(self):
        ItemQuestion.objects.create(
            organization=self.organization,
            question_id="13683341904",
            text="Tienen stock?",
            status="ANSWERED",
            answer_text="Si hay",
        )
        project_question(ItemQuestion.objects.get())
        conversation = Conversation.objects.get()
        page = self.client.get(f"/inbox/?conversation={conversation.pk}")
        self.assertNotContains(page, "Write a reply")
        self.assertNotContains(page, 'class="send-hint"')

    def test_another_organization_cannot_open_or_reply(self):
        ItemQuestion.objects.create(
            organization=self.organization,
            question_id="13683341904",
            text="Tienen stock?",
            status="UNANSWERED",
        )
        project_question(ItemQuestion.objects.get())
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

    def test_channel_query_still_hides_another_organization(self):
        project_question(
            ItemQuestion.objects.create(
                organization=self.organization,
                question_id="1",
                text="Mine",
                status="UNANSWERED",
            )
        )
        other = Organization.objects.create(name="Other")
        project_question(
            ItemQuestion.objects.create(
                organization=other,
                question_id="2",
                text="Only other",
                status="UNANSWERED",
            )
        )
        page = self.client.get("/inbox/?channel=not-a-channel")
        self.assertContains(page, "Mine")
        self.assertContains(page, "meli-itemquestions")
        self.assertNotContains(page, "Only other")

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


def refused(code, cause, message="Forbidden"):
    """An HTTPError carrying a Mercado Libre error body."""
    body = {"message": message, "error": "forbidden", "status": code, "cause": []}
    if cause:
        body["cause"] = [{"code": "x", "message": cause}]
    return HTTPError(
        "https://api.mercadolibre.com/",
        code,
        "refused",
        {},
        BytesIO(json.dumps(body).encode()),
    )


class _FakeRedis:
    def __init__(self):
        self.values = {}
        self.lists = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None):
        self.values[key] = value
        return True

    def delete(self, key):
        return int(self.values.pop(key, None) is not None)

    def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)
        return len(self.lists[key])

    def brpop(self, keys, timeout=0):
        if timeout <= 0:
            raise AssertionError("Redis reads a timeout of 0 as wait forever.")
        for key in [keys] if isinstance(keys, str) else keys:
            items = self.lists.get(key) or []
            if items:
                return (key, items.pop())
        return None


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
            if url.split("?")[0].endswith(f"/messages/{message['id']}"):
                self.assertIn("mark_as_read=false", url)
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
        self.assertContains(page, 'href="/inbox/"')
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

    def test_seller_reply_on_mercadolibre_clears_the_dot(self):
        created = "2026-11-01T12:00:00+00:00"
        answered = "2026-11-01T12:05:00+00:00"
        seller_message = {
            "id": "msg-2",
            "from": {"user_id": 468424240},
            "to": {"user_id": 99},
            "text": "Mide 67x60cm",
            "message_resources": [{"id": PACK, "name": "packs"}],
            "message_date": {"created": answered},
        }
        thread = {
            "conversation_status": {"status": "active"},
            "paging": {"total": 2, "limit": 50, "offset": 0},
            "messages": [
                sale_thread("Necesito medidas?", created=created)["messages"][0],
                seller_message,
            ],
        }

        def open_response(request, timeout=15):
            url = request.full_url
            if url.split("?")[0].endswith("/messages/msg-2"):
                return TokenResponse(seller_message)
            if "/messages/packs/" in url:
                return TokenResponse(thread)
            if f"/orders/{PACK}" in url:
                return TokenResponse(
                    sale_order(PACK, PACK, created, "Willian", "Guido", "Remera")
                )
            raise AssertionError(url)

        body = json.dumps(
            {
                **SAMPLE,
                "_id": "seller-reply",
                "topic": "messages",
                "resource": "/messages/msg-2",
            }
        )
        with patch("meli.client.urlopen", side_effect=open_response):
            response = self.client.post(
                WEBHOOK_URL,
                data=body,
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        conversation = Conversation.objects.get(external_id=PACK)
        self.assertEqual(conversation.organization, self.organization)
        self.assertFalse(conversation.needs_reply)
        self.assertEqual(
            conversation.messages.get(direction=Message.Direction.INBOUND).body,
            "Necesito medidas?",
        )
        self.assertEqual(
            conversation.messages.get(direction=Message.Direction.OUTBOUND).body,
            "Mide 67x60cm",
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
            if f"/messages/packs/{TAKEN}/" in url:
                return TokenResponse(sale_thread("Mio tambien", message_id="msg-taken"))
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
            "/inbox/?sync=ok",
        )
        self.assertFalse(Conversation.objects.filter(external_id=OLD).exists())
        kept = Conversation.objects.get(organization=other, external_id=TAKEN)
        self.assertEqual(kept.title, "Theirs")
        self.assertFalse(kept.messages.exists())
        copy = Conversation.objects.get(organization=self.organization, external_id=TAKEN)
        self.assertEqual(copy.title, "Other Buyer · Taken")
        loaded = Conversation.objects.get(external_id=PACK)
        self.assertEqual(loaded.organization, self.organization)
        self.assertEqual(loaded.title, "Willian Guido · Remera")
        self.assertEqual(loaded.messages.get().body, "Necesito medidas?")
        unread = Conversation.objects.get(external_id=UNREAD)
        self.assertEqual(unread.messages.get().body, "Mensaje viejo")
        self.assertFalse(any(OLD in url for url in calls if "/messages/packs/" in url))
        shown = self.client.get("/inbox/?sync=ok")
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
                "/inbox/?sync=queued",
            )
            self.assertEqual(opened.call_count, 0)
            self.assertEqual(fake.lists[QUEUE], [str(self.organization.pk)])
            again = self.client.post("/meli/postsale/sync/")
            self.assertRedirects(
                again,
                "/inbox/?sync=queued",
            )
            self.assertEqual(fake.lists[QUEUE], [str(self.organization.pk)])
            waiting = self.client.get("/inbox/")
            self.assertContains(waiting, "Post-sale messages are loading.")
            self.assertContains(waiting, 'hx-trigger="every 3s"')
            self.assertFalse(Conversation.objects.filter(external_id=PACK).exists())
            self.assertTrue(run_next_postsale_job(block_seconds=0))
            loaded = self.client.get("/inbox/")
        self.assertEqual(fake.values[status_key(self.organization.pk)], "ok")
        self.assertContains(loaded, "Post-sale messages loaded.")
        self.assertNotContains(loaded, "Post-sale messages are loading.")
        self.assertNotContains(loaded, 'id="inbox-status" hx-get=')
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
            f"/inbox/?conversation={conversation.pk}",
        )
        conversation.refresh_from_db()
        self.assertFalse(conversation.needs_reply)
        outbound = conversation.messages.get(direction=Message.Direction.OUTBOUND)
        self.assertEqual(outbound.body, "Mide 67x60cm")
        page = self.client.get(f"/inbox/?conversation={conversation.pk}")
        self.assertContains(page, "Mide 67x60cm")
        # A post-sale chat stays open; Mercado Libre decides what it accepts.
        self.assertContains(page, "Write a reply")
        with patch("meli.client.urlopen", side_effect=open_response):
            self.client.post(
                f"/inbox/{conversation.pk}/reply/",
                {"text": "Te paso mas fotos"},
            )
        posts = [url for url in calls if url.split("?")[0].endswith(f"/sellers/468424240")]
        self.assertGreaterEqual(len(posts), 2)

    def test_a_reply_to_a_known_buyer_is_one_call_and_shows_at_once(self):
        now = timezone.now()
        conversation = Conversation.objects.create(
            organization=self.organization,
            channel="meli-postsales",
            external_id=PACK,
            title="Willian Guido · Remera",
            link_url=f"https://www.mercadolibre.com.ar/ventas/{PACK}/detalle",
            needs_reply=True,
            last_message_at=now,
            counterpart_id="99",
        )
        Message.objects.create(
            organization=self.organization, conversation=conversation, direction=Message.Direction.INBOUND,
            body="Necesito medidas?", external_id="msg-1", sent_at=now,
        )
        calls = []

        def open_response(request, timeout=15):
            calls.append((request.get_method(), request.full_url))
            if getattr(request, "data", None):
                self.assertEqual(json.loads(request.data)["to"]["user_id"], 99)
                return TokenResponse({"id": "msg-2", "text": "Mide 67x60cm", "message_date": {"created": timezone.now().isoformat()}})
            if "/messages/packs/" in request.full_url:
                return TokenResponse({
                    "conversation_status": {"status": "active"},
                    "paging": {"total": 2},
                    "messages": [
                        sale_thread("Necesito medidas?", created=now.isoformat())["messages"][0],
                        {"id": "msg-2", "from": {"user_id": 468424240}, "to": {"user_id": 99}, "text": "Mide 67x60cm",
                         "message_date": {"created": timezone.now().isoformat()}},
                    ],
                })
            raise AssertionError(request.full_url)

        with patch("meli.client.urlopen", side_effect=open_response):
            self.client.post(f"/inbox/{conversation.pk}/reply/", {"text": "Mide 67x60cm"})
        # The post comes first: no reading the chat to find the buyer, and no order fetch afterwards.
        self.assertEqual(calls[0][0], "POST")
        self.assertFalse(any("/orders/" in url for _method, url in calls))
        outbound = conversation.messages.get(direction=Message.Direction.OUTBOUND)
        self.assertEqual((outbound.external_id, outbound.body), ("msg-2", "Mide 67x60cm"))
        self.assertFalse(conversation.messages.filter(external_id="pending").exists())
        conversation.refresh_from_db()
        self.assertFalse(conversation.needs_reply)

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
            f"/inbox/?conversation={conversation.pk}&reply=failed",
        )
        conversation.refresh_from_db()
        self.assertTrue(conversation.needs_reply)
        self.assertFalse(
            conversation.messages.filter(direction=Message.Direction.OUTBOUND).exists()
        )
        page = self.client.get(response.url)
        self.assertContains(page, "Mercado Libre could not be reached. Try again.")
        self.assertContains(page, "Write a reply")

    def test_mercadolibre_refusal_is_shown_and_the_chat_stays_open(self):
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
                raise refused(403, "Conversation closed")
            if "/messages/packs/" in request.full_url:
                return TokenResponse(sale_thread("Necesito medidas?", created=now.isoformat()))
            raise AssertionError(request.full_url)

        with patch("meli.client.urlopen", side_effect=open_response):
            self.client.post(f"/inbox/{conversation.pk}/reply/", {"text": "Mide 67x60cm"})
        self.assertFalse(conversation.messages.filter(external_id="pending").exists())
        page = self.client.get(f"/inbox/?conversation={conversation.pk}")
        self.assertContains(
            page,
            "Mercado Libre did not accept the message: Conversation closed",
        )
        self.assertContains(page, '<blockquote class="unsent">Mide 67x60cm</blockquote>')
        self.assertContains(page, "Write a reply")
        self.assertNotContains(page, 'data-sending="true"')
        self.assertNotContains(page, "access-1")
        before = page.context["list_version"]

        stranger = User.objects.create_user("eve", email="eve@example.com")
        other = Organization.objects.create(name="Other")
        Membership.objects.create(user=stranger, organization=other, role=Membership.Role.OWNER)
        self.client.force_login(stranger)
        hidden = self.client.get(
            f"/inbox/?conversation={conversation.pk}",
            HTTP_HX_REQUEST="true",
            HTTP_HX_TARGET="thread",
        )
        self.assertNotContains(hidden, "Conversation closed")
        self.assertNotContains(hidden, "Mide 67x60cm")
        blocked = self.client.post(f"/inbox/{conversation.pk}/reply/", {"text": "x"})
        self.assertEqual(blocked.status_code, 404)

        self.client.force_login(self.user)
        from meli.jobs import clear_reply_error

        clear_reply_error(conversation)
        after = self.client.get(f"/inbox/?conversation={conversation.pk}")
        self.assertNotContains(after, "Conversation closed")
        self.assertNotEqual(after.context["list_version"], before)

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
            f"/inbox/?conversation={conversation.pk}&reply=failed",
        )
        conversation.refresh_from_db()
        self.assertTrue(conversation.needs_reply)

    def test_open_post_sale_stays_selected_and_shows_every_message(self):
        now = timezone.now()
        first = Conversation.objects.create(
            organization=self.organization,
            channel="meli-itemquestions",
            external_id="anillo",
            title="Anillo Acero",
            needs_reply=True,
            last_message_at=now,
        )
        sale = Conversation.objects.create(
            organization=self.organization,
            channel="meli-postsales",
            external_id=PACK,
            title="Willian sale",
            needs_reply=True,
            last_message_at=now - timedelta(hours=1),
        )
        Message.objects.create(
            organization=self.organization,
            conversation=sale,
            direction=Message.Direction.INBOUND,
            body="Necesito medidas?",
            external_id="m1",
            sent_at=now - timedelta(hours=2),
        )
        Message.objects.create(
            organization=self.organization,
            conversation=sale,
            direction=Message.Direction.OUTBOUND,
            body="Mide 67x60cm",
            external_id="m2",
            sent_at=now - timedelta(hours=1),
        )
        other = Organization.objects.create(name="Other")
        hidden = Conversation.objects.create(
            organization=other,
            channel="meli-postsales",
            external_id="hidden-pack",
            title="Hidden sale",
            needs_reply=True,
            last_message_at=now,
        )
        Message.objects.create(
            organization=other,
            conversation=hidden,
            direction=Message.Direction.INBOUND,
            body="Only other org",
            external_id="secret",
            sent_at=now,
        )
        page = self.client.get(f"/inbox/?conversation={sale.pk}")
        content = page.content.decode()
        self.assertIn(f'id="conversation-{sale.pk}" class="active"', content)
        self.assertIn(f'id="conversation-{first.pk}" class=""', content)
        self.assertIn(f'data-open="{sale.pk}"', content)
        self.assertIn(f'data-conversation="{sale.pk}"', content)
        self.assertIn(f'data-requested="{sale.pk}"', content)
        self.assertIn(f'id="conversation-body-{sale.pk}"', content)
        self.assertNotIn("hx-sync=", content)
        self.assertIn('inbox.setAttribute("data-open"', content)
        self.assertIn("removeAttribute('data-open')", content)
        self.assertIn('rows[i].classList.add("active")', content)
        self.assertIn('document.getElementById("thread")', content)
        self.assertNotIn('target.getAttribute("data-conversation")', content)
        self.assertNotIn("inboxDebug", content)
        self.assertLess(
            content.index(f'id="conversation-{sale.pk}"'),
            content.index(f'id="conversation-{first.pk}"'),
        )
        self.assertIn('hx-on:htmx:config-request="inboxSelectRow(this)"', content)
        self.assertIn('id="thread-loading"', content)
        self.assertIn("Loading conversation", content)
        self.assertContains(page, "Necesito medidas?")
        self.assertContains(page, "Mide 67x60cm")
        self.assertNotContains(page, "Only other org")
        self.assertIn('id="composer-slot"', content)
        marker = f'id="conversation-body-{sale.pk}"'
        body = content.split(marker, 1)[1].split('id="composer-slot"', 1)[0]
        self.assertIn("Willian sale", body)
        self.assertIn("meli-postsales", body)
        self.assertIn("Necesito medidas?", body)
        self.assertIn("Mide 67x60cm", body)
        self.assertNotIn("Anillo Acero", body)
        self.assertNotIn('id="composer-text"', body)
        self.assertNotIn("hx-trigger", body)
        self.assertIn("inboxPollUrl", content)
        polled = self.client.get(
            f"/inbox/?conversation={sale.pk}",
            headers={"HX-Request": "true", "HX-Target": "chat-list"},
        )
        polled_html = polled.content.decode()
        self.assertTrue(polled_html.startswith('<aside id="chat-list"'))
        self.assertNotIn("<html", polled_html)
        self.assertNotIn('class="sidebar"', polled_html)
        self.assertNotIn("Hidden sale", polled_html)
        self.assertLess(
            polled_html.index(f'id="conversation-{sale.pk}" class="active"'),
            polled_html.index(f'id="conversation-{first.pk}" class=""'),
        )
        self.assertIn(f'id="conversation-body-{sale.pk}" hx-swap-oob="outerHTML"', polled_html)
        self.assertIn('id="composer-slot" hx-swap-oob="outerHTML"', polled_html)
        polled_body = polled_html.split(marker, 1)[1].split('id="composer-slot"', 1)[0]
        self.assertIn("Willian sale", polled_body)
        self.assertIn("meli-postsales", polled_body)
        self.assertIn("Necesito medidas?", polled_body)
        self.assertIn("Mide 67x60cm", polled_body)
        self.assertNotIn("Anillo Acero", polled_body)
        self.assertNotIn("Only other org", polled_html)

    def test_thread_click_returns_only_the_thread_and_skips_the_nav(self):
        now = timezone.now()
        sale = Conversation.objects.create(
            organization=self.organization,
            channel="meli-postsales",
            external_id=PACK,
            title="Willian sale",
            needs_reply=True,
            last_message_at=now,
        )
        other = Organization.objects.create(name="Other")
        hidden = Conversation.objects.create(
            organization=other,
            channel="meli-postsales",
            external_id="hidden-pack",
            title="Hidden sale",
            needs_reply=True,
            last_message_at=now,
        )
        headers = {"HX-Request": "true", "HX-Target": "thread"}
        with self.assertNumQueries(6):
            clicked = self.client.get(f"/inbox/?conversation={sale.pk}", headers=headers)
        html = clicked.content.decode()
        self.assertTrue(html.startswith(f'<section id="thread" class="chat-thread" data-conversation="{sale.pk}"'))
        self.assertNotIn('class="sidebar"', html)
        self.assertIn(f'id="conversation-{sale.pk}" hx-swap-oob="outerHTML" class="active"', html)
        stolen = self.client.get(f"/inbox/?conversation={hidden.pk}", headers=headers)
        self.assertNotContains(stolen, "Hidden sale")
        self.assertContains(stolen, "Willian sale")
        stolen_list = self.client.get(
            f"/inbox/?conversation={hidden.pk}",
            headers={"HX-Request": "true", "HX-Target": "chat-list"},
        )
        self.assertNotContains(stolen_list, "Hidden sale")
        self.assertContains(stolen_list, f'id="conversation-{sale.pk}" class="active"')

    def test_list_poll_answers_204_until_something_changes(self):
        now = timezone.now()
        sale = Conversation.objects.create(
            organization=self.organization,
            channel="meli-postsales",
            external_id=PACK,
            title="Willian sale",
            needs_reply=True,
            last_message_at=now,
        )
        question = Conversation.objects.create(
            organization=self.organization,
            channel="meli-itemquestions",
            external_id="anillo",
            title="Anillo Acero",
            needs_reply=True,
            last_message_at=now,
        )
        other = Organization.objects.create(name="Other")
        hidden = Conversation.objects.create(
            organization=other,
            channel="meli-postsales",
            external_id="hidden-pack",
            title="Hidden sale",
            needs_reply=True,
            last_message_at=now,
        )
        poll = {"HX-Request": "true", "HX-Target": "chat-list"}
        url = f"/inbox/?conversation={sale.pk}"
        first = self.client.get(url, headers=poll)
        self.assertEqual(first.status_code, 200)
        version = re.search(r'data-version="([0-9a-f]+)"', first.content.decode()).group(1)
        same = self.client.get(url, headers={**poll, "X-Inbox-Version": version})
        self.assertEqual(same.status_code, 204)
        self.assertEqual(same.content, b"")
        Message.objects.create(
            organization=other,
            conversation=hidden,
            direction=Message.Direction.INBOUND,
            body="Only other org",
            external_id="secret",
            sent_at=now,
        )
        still = self.client.get(url, headers={**poll, "X-Inbox-Version": version})
        self.assertEqual(still.status_code, 204)
        moved = self.client.get(
            f"/inbox/?conversation={question.pk}",
            headers={**poll, "X-Inbox-Version": version},
        )
        self.assertEqual(moved.status_code, 200)
        Message.objects.create(
            organization=self.organization,
            conversation=sale,
            direction=Message.Direction.INBOUND,
            body="Hay talle M?",
            external_id="m-new",
            sent_at=now,
        )
        changed = self.client.get(url, headers={**poll, "X-Inbox-Version": version})
        self.assertEqual(changed.status_code, 200)
        self.assertContains(changed, "Hay talle M?")
        self.assertNotContains(changed, "Only other org")
        self.assertNotContains(changed, "Hidden sale")
        page = self.client.get(url).content.decode()
        self.assertIn("visibilityState", page)
        self.assertIn("X-Inbox-Version", page)
        latest = re.search(r'data-version="([0-9a-f]+)"', changed.content.decode()).group(1)
        self.assertNotEqual(latest, version)
        self.assertIn(f'data-version="{latest}"', page)

    def test_redis_client_is_reused_per_url(self):
        from meli.jobs import _client

        with override_settings(REDIS_URL="redis://reuse-test"):
            first = _client()
            self.assertIs(_client(), first)
        with override_settings(REDIS_URL="redis://other-test"):
            self.assertIsNot(_client(), first)

    def test_profile_menu_still_gets_the_nav(self):
        menu = self.client.get("/profile/menu/", headers={"HX-Request": "true"})
        self.assertEqual(menu.status_code, 200)
        self.assertContains(menu, "ada@example.com")

    def test_phone_layout_shows_one_pane_and_keeps_it_across_polls(self):
        now = timezone.now()
        mine = Conversation.objects.create(
            organization=self.organization,
            channel="meli-postsales",
            external_id=PACK,
            title="Willian sale",
            needs_reply=True,
            last_message_at=now,
        )
        other = Organization.objects.create(name="Other")
        hidden = Conversation.objects.create(
            organization=other,
            channel="meli-postsales",
            external_id="hidden-pack",
            title="Hidden sale",
            needs_reply=True,
            last_message_at=now,
        )
        listing = self.client.get("/inbox/").content.decode()
        self.assertIn('data-view="list"', listing)
        self.assertIn('class="menu-toggle"', listing)
        self.assertIn('class="nav-backdrop"', listing)
        self.assertIn("@media (max-width: 768px)", listing)
        self.assertIn("@media (hover: hover)", listing)
        opened = self.client.get(f"/inbox/?conversation={mine.pk}").content.decode()
        self.assertIn('data-view="thread"', opened)
        self.assertIn('class="thread-back" onclick="inboxShowList()"', opened)
        self.assertIn('inbox.setAttribute("data-view", "thread")', opened)
        self.assertIn("htmx:historyRestore", opened)
        polled = self.client.get(
            f"/inbox/?conversation={mine.pk}",
            headers={"HX-Request": "true", "HX-Target": "chat-list"},
        ).content.decode()
        self.assertNotIn("data-view", polled)
        self.assertNotIn('id="inbox"', polled)
        thread = self.client.get(
            f"/inbox/?conversation={mine.pk}",
            headers={"HX-Request": "true", "HX-Target": "thread"},
        ).content.decode()
        self.assertNotIn("data-view", thread)
        self.assertIn("thread-back", thread)
        foreign = self.client.get(f"/inbox/?conversation={hidden.pk}").content.decode()
        self.assertNotIn("Hidden sale", foreign)
        self.assertNotIn(f'id="conversation-{hidden.pk}"', foreign)
        self.assertNotIn(f'id="conversation-body-{hidden.pk}"', foreign)
        self.assertNotIn(f'data-open="{hidden.pk}"', foreign)
        self.assertIn(f'data-open="{mine.pk}"', foreign)
        self.assertIn(f'data-conversation="{mine.pk}"', foreign)
        members = self.client.get("/organization/members/")
        self.assertContains(members, 'class="table-scroll"')

    def _bare_message_api(self, message_id, calls, wrapped=False, resource="packs"):
        created = timezone.now().isoformat()
        message = {
            "id": message_id,
            "from": {"user_id": 99},
            "to": {"user_id": 468424240},
            "text": "Llega mañana?",
            "message_resources": [{"id": PACK, "name": resource}],
            "message_date": {"created": created},
        }
        if wrapped:
            # Mercado Libre returns one message the way it returns a thread.
            message = {
                "paging": {"limit": 10, "offset": 0, "total": 1},
                "conversation_status": {"status": "active"},
                "messages": [message],
                "seller_max_message_length": 350,
            }
        order = sale_order(PACK, PACK, created, "Willian", "Guido", "Remera")

        def open_response(request, timeout=15):
            url = request.full_url
            calls.append(url)
            if url.split("?")[0].endswith(f"/messages/{message_id}"):
                return TokenResponse(message)
            if "/messages/packs/" in url:
                return TokenResponse(sale_thread("Llega mañana?", message_id=message_id, created=created))
            if f"/orders/{PACK}" in url:
                return TokenResponse(order)
            raise AssertionError(url)

        return open_response

    def test_bare_message_id_webhook_shows_the_chat_on_the_next_poll(self):
        bare = "01a1a4064bb97f29ad2dcde436332268"
        before = self.client.get(
            "/inbox/", headers={"HX-Request": "true", "HX-Target": "chat-list"}
        )
        version = re.search(r'data-version="([^"]+)"', before.content.decode()).group(1)
        calls = []
        body = json.dumps({**SAMPLE, "_id": "bare-note", "topic": "messages", "resource": bare})
        with patch("meli.client.urlopen", side_effect=self._bare_message_api(bare, calls)):
            response = self.client.post(WEBHOOK_URL, data=body, content_type="application/json")
        self.assertEqual(response.status_code, 200)
        fetched = [url for url in calls if url.split("?")[0].endswith(f"/messages/{bare}")]
        self.assertEqual(len(fetched), 1)
        self.assertIn("mark_as_read=false", fetched[0])
        self.assertIn("tag=post_sale", fetched[0])
        conversation = Conversation.objects.get(organization=self.organization, external_id=PACK)
        self.assertEqual(conversation.messages.get().body, "Llega mañana?")
        polled = self.client.get(
            "/inbox/",
            headers={"HX-Request": "true", "HX-Target": "chat-list", "X-Inbox-Version": version},
        )
        self.assertEqual(polled.status_code, 200)
        self.assertContains(polled, f'id="conversation-{conversation.pk}"')

    def test_bare_message_id_in_a_messages_list_still_loads(self):
        bare = "01a1a7d0c52b777da9dca3003a4248af"
        calls = []
        body = json.dumps({**SAMPLE, "_id": "wrapped-note", "topic": "messages", "resource": bare})
        with patch(
            "meli.client.urlopen",
            side_effect=self._bare_message_api(bare, calls, wrapped=True),
        ):
            self.client.post(WEBHOOK_URL, data=body, content_type="application/json")
        conversation = Conversation.objects.get(organization=self.organization, external_id=PACK)
        self.assertEqual(conversation.messages.get().body, "Llega mañana?")

    def test_a_sale_without_a_pack_loads_by_its_order(self):
        bare = "01a1a7d0c52b777da9dca3003a4248b0"
        calls = []
        body = json.dumps({**SAMPLE, "_id": "order-note", "topic": "messages", "resource": bare})
        with patch(
            "meli.client.urlopen",
            side_effect=self._bare_message_api(bare, calls, wrapped=True, resource="orders"),
        ):
            self.client.post(WEBHOOK_URL, data=body, content_type="application/json")
        self.assertTrue(
            Conversation.objects.filter(organization=self.organization, external_id=PACK).exists()
        )

    def test_a_message_naming_no_sale_is_logged(self):
        bare = "01a1a7d0c52b777da9dca3003a4248b1"
        body = json.dumps({**SAMPLE, "_id": "lost-note", "topic": "messages", "resource": bare})

        def open_response(request, timeout=15):
            return TokenResponse({"messages": [{"id": bare, "text": "Hola"}]})

        with (
            patch("meli.client.urlopen", side_effect=open_response),
            self.assertLogs("meli", "WARNING") as logs,
        ):
            self.client.post(WEBHOOK_URL, data=body, content_type="application/json")
        self.assertIn("names no pack or order", "\n".join(logs.output))
        self.assertFalse(Conversation.objects.exists())

    def test_bare_message_id_for_another_seller_stays_out(self):
        bare = "02b2b5175cc08f30be3edef547443379"
        other = connect_seller(user_id="555")
        MercadoLibreAccount.objects.filter(organization=other).update(
            access_token=encrypt_secret("access-2"),
            refresh_token=encrypt_secret("refresh-2"),
        )
        calls = []
        body = json.dumps(
            {**SAMPLE, "_id": "other-note", "topic": "messages", "resource": bare, "user_id": 555}
        )
        with patch("meli.client.urlopen", side_effect=self._bare_message_api(bare, calls)):
            self.client.post(WEBHOOK_URL, data=body, content_type="application/json")
        self.assertFalse(Conversation.objects.filter(organization=self.organization).exists())
        hidden = Conversation.objects.get(organization=other)
        page = self.client.get("/inbox/")
        self.assertNotContains(page, str(hidden.pk))
        self.assertNotContains(page, "Llega mañana?")
        polled = self.client.get("/inbox/", headers={"HX-Request": "true", "HX-Target": "chat-list"})
        self.assertNotContains(polled, str(hidden.pk))

    def test_webhook_queues_the_load_and_answers_without_calling_mercadolibre(self):
        from meli.jobs import NOTIFICATION_QUEUE, run_next_notification_job

        bare = "03c3c6286dd19a41cf4fef0658554480"
        fake = _FakeRedis()
        body = json.dumps({**SAMPLE, "_id": "queued-note", "topic": "messages", "resource": bare})
        with (
            override_settings(REDIS_URL="redis://queue-test"),
            patch("meli.jobs._under_test", return_value=False),
            patch("meli.jobs._client", return_value=fake),
            patch("meli.client.urlopen", side_effect=AssertionError("network")),
        ):
            response = self.client.post(WEBHOOK_URL, data=body, content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(fake.lists[NOTIFICATION_QUEUE]), 1)
        job = json.loads(fake.lists[NOTIFICATION_QUEUE][0])
        self.assertEqual(job["resource"], bare)
        self.assertFalse(Conversation.objects.exists())
        calls = []
        with (
            patch("meli.jobs._client", return_value=fake),
            patch("meli.client.urlopen", side_effect=self._bare_message_api(bare, calls)),
        ):
            self.assertTrue(run_next_notification_job(block_seconds=0))
        self.assertTrue(
            Conversation.objects.filter(organization=self.organization, external_id=PACK).exists()
        )
        self.assertEqual(fake.lists[NOTIFICATION_QUEUE], [])

    def test_refresh_loads_a_missed_bare_notification_from_the_last_two_days(self):
        from meli.jobs import refresh_unanswered

        bare = "04d4d7397ee20b52d05f0f1769665591"
        old = "05e5e8408ff31c63e16f1f2870776602"
        MercadoLibreNotification.objects.create(
            organization=self.organization,
            notification_id="missed",
            topic="messages",
            resource=bare,
            user_id="468424240",
            payload={},
        )
        stale = MercadoLibreNotification.objects.create(
            organization=self.organization,
            notification_id="stale",
            topic="messages",
            resource=old,
            user_id="468424240",
            payload={},
        )
        MercadoLibreNotification.objects.filter(pk=stale.pk).update(
            created_at=timezone.now() - timedelta(days=3)
        )
        calls = []
        with patch("meli.client.urlopen", side_effect=self._bare_message_api(bare, calls)):
            refresh_unanswered(self.organization.pk)
        self.assertTrue(
            Conversation.objects.filter(organization=self.organization, external_id=PACK).exists()
        )
        self.assertFalse(any(old in url for url in calls))

    def test_post_sale_header_links_to_the_sale_page(self):
        SellerProfile.objects.create(organization=self.organization, meli_user_id="468424240", site_id="MLA")
        calls = []
        bare = "06f6f9519aa42d74f1a0a2a3981887713"
        body = json.dumps({**SAMPLE, "_id": "link-note", "topic": "messages", "resource": bare})
        with patch("meli.client.urlopen", side_effect=self._bare_message_api(bare, calls)):
            self.client.post(WEBHOOK_URL, data=body, content_type="application/json")
        conversation = Conversation.objects.get(organization=self.organization, external_id=PACK)
        link = f"https://www.mercadolibre.com.ar/ventas/{PACK}/detalle"
        self.assertEqual(conversation.link_url, link)
        self.assertNotIn("mensajes", link)
        page = self.client.get(f"/inbox/?conversation={conversation.pk}")
        self.assertContains(page, f'<a href="{link}" target="_blank" rel="noopener">')

    def test_post_sale_without_a_seller_profile_has_no_link(self):
        from meli.postsale import sale_link

        self.assertEqual(sale_link("", PACK), "")
        self.assertEqual(sale_link("XXX", PACK), "")
        self.assertEqual(sale_link("MLA", "not-a-number"), "")
        calls = []
        bare = "07a7a0620bb53e85a2b1b3b4092998824"
        body = json.dumps({**SAMPLE, "_id": "nolink-note", "topic": "messages", "resource": bare})
        with patch("meli.client.urlopen", side_effect=self._bare_message_api(bare, calls)):
            self.client.post(WEBHOOK_URL, data=body, content_type="application/json")
        conversation = Conversation.objects.get(organization=self.organization, external_id=PACK)
        self.assertEqual(conversation.link_url, "")

    def test_sale_link_migration_uses_each_organizations_own_site(self):
        import importlib

        from django.apps import apps

        migration = importlib.import_module("meli.migrations.0007_postsale_sale_links")
        SellerProfile.objects.create(organization=self.organization, meli_user_id="468424240", site_id="MLA")
        mine = Conversation.objects.create(
            organization=self.organization, channel="meli-postsales", external_id=PACK, title="Mine"
        )
        other = Organization.objects.create(name="Other")
        SellerProfile.objects.create(organization=other, meli_user_id="555", site_id="MLM")
        theirs = Conversation.objects.create(
            organization=other, channel="meli-postsales", external_id=TAKEN, title="Theirs"
        )
        kept = Conversation.objects.create(
            organization=other,
            channel="meli-postsales",
            external_id=UNREAD,
            title="Kept",
            link_url="https://example.com/kept",
        )
        migration.link_sales(apps, None)
        mine.refresh_from_db()
        theirs.refresh_from_db()
        kept.refresh_from_db()
        self.assertEqual(mine.link_url, f"https://www.mercadolibre.com.ar/ventas/{PACK}/detalle")
        self.assertEqual(theirs.link_url, f"https://www.mercadolibre.com.mx/ventas/{TAKEN}/detalle")
        self.assertEqual(kept.link_url, "https://example.com/kept")

    def test_pack_keeps_every_page_of_the_conversation(self):
        from meli.postsale import load_pack

        created = "2026-11-01T12:00:00+00:00"

        def message(index):
            return {
                "id": f"msg-{index}",
                "from": {"user_id": 99 if index % 2 else 468424240},
                "to": {"user_id": 468424240 if index % 2 else 99},
                "text": f"Line {index}",
                "message_date": {"created": f"2026-11-01T12:00:{index:02d}+00:00"},
            }

        def open_response(request, timeout=15):
            url = request.full_url
            if "/messages/packs/" in url:
                offset = int(parse_qs(urlparse(url).query)["offset"][0])
                batch = [
                    message(index) for index in range(offset, min(offset + 50, 51))
                ]
                return TokenResponse(
                    {
                        "conversation_status": {"status": "active"},
                        "paging": {"total": 51, "limit": 50, "offset": offset},
                        "messages": batch,
                    }
                )
            if f"/orders/{PACK}" in url:
                return TokenResponse(
                    sale_order(PACK, PACK, created, "Willian", "Guido", "Remera")
                )
            raise AssertionError(url)

        with patch("meli.client.urlopen", side_effect=open_response):
            load_pack(Organization.objects.get(pk=self.organization.pk), PACK)
        conversation = Conversation.objects.get(external_id=PACK)
        self.assertEqual(conversation.messages.count(), 51)
        page = self.client.get(f"/inbox/?conversation={conversation.pk}")
        self.assertContains(page, "Line 0")
        self.assertContains(page, "Line 50")
        transcript = page.content.decode().split(f'id="conversation-body-{conversation.pk}"', 1)[1]
        self.assertLess(transcript.index("Line 0"), transcript.index("Line 50"))

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


def shared_seller(name, user_id="468424240", expires_in=timedelta(hours=1)):
    organization = Organization.objects.create(name=name)
    MercadoLibreAccount.objects.create(
        organization=organization,
        meli_user_id=user_id,
        access_token=encrypt_secret("access-1"),
        refresh_token=encrypt_secret("refresh-1"),
        expires_at=timezone.now() + expires_in,
    )
    return organization


@override_settings(
    MELI_WEBHOOK_SECRET=WEBHOOK_SECRET,
    MELI_CLIENT_ID=str(SAMPLE["application_id"]),
)
class SharedSellerTests(TestCase):
    """One Mercado Libre seller connected to two organizations."""

    def setUp(self):
        self.first = shared_seller("First")
        self.second = shared_seller("Second")
        self.stranger = shared_seller("Stranger", user_id="555")

    def _post(self, payload):
        return self.client.post(
            WEBHOOK_URL,
            data=json.dumps({**SAMPLE, **payload}),
            content_type="application/json",
        )

    def test_a_refresh_gives_every_organization_the_new_tokens(self):
        MercadoLibreAccount.objects.filter(meli_user_id="468424240").update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        fresh = {**TOKEN, "access_token": "access-2", "refresh_token": "refresh-2"}
        with patch("meli.client.urlopen", return_value=TokenResponse(fresh)) as urlopen:
            first = MercadoLibreAccount.objects.get(organization=self.first)
            self.assertEqual(first.fresh_access_token(), "access-2")
            second = MercadoLibreAccount.objects.get(organization=self.second)
            self.assertEqual(second.fresh_access_token(), "access-2")
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(decrypt_secret(second.refresh_token), "refresh-2")
        stranger = MercadoLibreAccount.objects.get(organization=self.stranger)
        self.assertEqual(decrypt_secret(stranger.access_token), "access-1")

    def test_a_question_reaches_every_organization_with_that_seller(self):
        with patch("meli.client.urlopen", side_effect=question_api):
            response = self._post(
                {
                    "_id": "shared-question",
                    "topic": "questions",
                    "resource": "/questions/13683341904",
                }
            )
        self.assertEqual(response.status_code, 200)
        notes = MercadoLibreNotification.objects.filter(notification_id="shared-question")
        self.assertEqual(
            {note.organization_id for note in notes},
            {self.first.id, self.second.id},
        )
        for organization in (self.first, self.second):
            question = ItemQuestion.objects.get(organization=organization)
            self.assertEqual(question.text, "Tienen stock?")
            conversation = Conversation.objects.get(organization=organization)
            self.assertEqual(conversation.external_id, "13683341904")
        self.assertFalse(ItemQuestion.objects.filter(organization=self.stranger).exists())
        self.assertFalse(Conversation.objects.filter(organization=self.stranger).exists())
        self.assertFalse(
            MercadoLibreNotification.objects.filter(organization=self.stranger).exists()
        )

    def test_a_post_sale_message_reaches_every_organization_with_that_seller(self):
        created = timezone.now().isoformat()

        def open_response(request, timeout=15):
            url = request.full_url
            if f"/messages/packs/{PACK}/" in url:
                return TokenResponse(sale_thread("Necesito medidas?", created=created))
            if f"/orders/{PACK}" in url:
                return TokenResponse(
                    sale_order(PACK, PACK, created, "Willian", "Guido", "Remera")
                )
            raise AssertionError(url)

        with patch("meli.client.urlopen", side_effect=open_response):
            self._post(
                {
                    "_id": "shared-message",
                    "topic": "messages",
                    "resource": f"/packs/{PACK}",
                }
            )
        for organization in (self.first, self.second):
            conversation = Conversation.objects.get(
                organization=organization,
                external_id=PACK,
            )
            self.assertEqual(conversation.messages.get().body, "Necesito medidas?")
            self.assertEqual(conversation.messages.get().organization, organization)
        self.assertFalse(Conversation.objects.filter(organization=self.stranger).exists())

    def test_disconnecting_one_organization_keeps_the_other_connected(self):
        owner = User.objects.create_user("ada", email="ada@example.com")
        Membership.objects.create(
            user=owner,
            organization=self.first,
            role=Membership.Role.OWNER,
        )
        self.client.force_login(owner)
        self.client.post("/meli/disconnect/")
        self.assertFalse(MercadoLibreAccount.objects.filter(organization=self.first).exists())
        self.assertTrue(MercadoLibreAccount.objects.filter(organization=self.second).exists())
        with patch("meli.client.urlopen", side_effect=question_api):
            self._post(
                {
                    "_id": "after-disconnect",
                    "topic": "questions",
                    "resource": "/questions/13683341904",
                }
            )
        self.assertTrue(ItemQuestion.objects.filter(organization=self.second).exists())
        self.assertFalse(ItemQuestion.objects.filter(organization=self.first).exists())


def unreadable_token():
    """A token encrypted under another SECRET_KEY, like one saved by another server."""
    from cryptography.fernet import Fernet

    return Fernet(Fernet.generate_key()).encrypt(b"access-elsewhere").decode()


@override_settings(
    MELI_WEBHOOK_SECRET=WEBHOOK_SECRET,
    MELI_CLIENT_ID=str(SAMPLE["application_id"]),
)
class SeparateEnvironmentTests(TestCase):
    """A laptop and Render share Upstash and Neon but not SECRET_KEY."""

    def setUp(self):
        self.organization = shared_seller("Test Seller")
        MercadoLibreAccount.objects.filter(organization=self.organization).update(
            access_token=unreadable_token()
        )
        self.user = User.objects.create_user("ada", email="ada@example.com")
        Membership.objects.create(
            user=self.user,
            organization=self.organization,
            role=Membership.Role.OWNER,
        )
        self.client.force_login(self.user)

    def test_every_redis_key_uses_this_environments_prefix(self):
        from meli import jobs

        from django.conf import settings

        self.assertEqual(jobs.REPLY_QUEUE, f"{settings.REDIS_PREFIX}:reply:queue")
        with override_settings(REDIS_PREFIX="vimple-dev"):
            self.assertEqual(jobs._key("reply", "queue"), "vimple-dev:reply:queue")
            self.assertEqual(jobs.reply_status_key("c1"), "vimple-dev:reply:status:c1")
            self.assertEqual(jobs.reply_error_key("c1"), "vimple-dev:reply:error:c1")
            self.assertEqual(jobs.status_key("o1"), "vimple-dev:postsale:status:o1")
            self.assertEqual(jobs.refresh_status_key("o1"), "vimple-dev:refresh:status:o1")

    def test_a_bad_prefix_stops_startup(self):
        from django.core.exceptions import ImproperlyConfigured

        from vimple.settings import redis_prefix

        self.assertEqual(redis_prefix(None), "vimple")
        self.assertEqual(redis_prefix(" vimple-dev "), "vimple-dev")
        for bad in ("bad prefix!", "Vimple", "x" * 33):
            with self.assertRaises(ImproperlyConfigured):
                redis_prefix(bad)

    def test_an_unreadable_token_is_logged_not_dropped(self):
        with (
            patch("meli.client.urlopen") as urlopen,
            self.assertLogs("meli", "WARNING") as logs,
        ):
            self.client.post(
                WEBHOOK_URL,
                data=json.dumps(
                    {
                        **SAMPLE,
                        "user_id": 468424240,
                        "_id": "unreadable",
                        "topic": "messages",
                        "resource": "01a1a7aa62c078d7a5b7844501f85b47",
                    }
                ),
                content_type="application/json",
            )
        self.assertEqual(urlopen.call_count, 0)
        line = "\n".join(logs.output)
        self.assertIn(str(self.organization.pk), line)
        self.assertIn("InvalidToken", line)
        self.assertNotIn("access-elsewhere", line)
        self.assertFalse(Conversation.objects.exists())
        self.assertTrue(
            MercadoLibreNotification.objects.filter(notification_id="unreadable").exists()
        )

    def test_a_reply_with_an_unreadable_token_says_so(self):
        conversation = Conversation.objects.create(
            organization=self.organization,
            channel="meli-postsales",
            external_id=PACK,
            title="Test Test",
            needs_reply=True,
            last_message_at=timezone.now(),
        )
        with patch("meli.client.urlopen") as urlopen:
            self.client.post(f"/inbox/{conversation.pk}/reply/", {"text": "Hola"})
        self.assertEqual(urlopen.call_count, 0)
        self.assertFalse(conversation.messages.filter(external_id="pending").exists())
        page = self.client.get(f"/inbox/?conversation={conversation.pk}")
        self.assertContains(
            page,
            "The saved Mercado Libre connection can&#x27;t be read on this server.",
        )



class WorkerLaneTests(TestCase):
    def test_the_worker_starts_a_reply_lane_and_a_general_lane(self):
        from meli import jobs

        started = []

        class FakeThread:
            def __init__(self, target, args, name, daemon):
                started.append((name, args[0]))

            def start(self):
                pass

        with (
            patch.object(jobs, "_started", False),
            patch.object(jobs, "postsale_queue_enabled", return_value=True),
            patch.object(jobs, "_skip_worker", return_value=False),
            patch("meli.jobs.threading.Thread", FakeThread),
        ):
            jobs.start_postsale_worker()
        self.assertEqual(started, [("meli-replies", jobs.REPLY_LANE), ("meli-jobs", jobs.WORKER_QUEUES)])
        self.assertEqual(jobs.REPLY_LANE, (jobs.REPLY_QUEUE,))
        self.assertNotIn(jobs.REPLY_QUEUE, jobs.WORKER_QUEUES)
