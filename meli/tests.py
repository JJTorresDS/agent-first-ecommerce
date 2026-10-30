import json
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from accounts.models import Membership, Organization
from meli.models import MercadoLibreAccount, MercadoLibreNotification

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


class MercadoLibreWebhookTests(TestCase):
    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)

    def test_stores_an_orders_notification(self):
        response = self.client.post(
            "/meli/webhooks/",
            data=json.dumps(SAMPLE),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok")
        row = MercadoLibreNotification.objects.get()
        self.assertEqual(row.notification_id, SAMPLE["_id"])
        self.assertEqual(row.topic, "orders_v2")
        self.assertEqual(row.resource, "/orders/2195160686")
        self.assertEqual(row.user_id, "468424240")
        self.assertIsNone(row.organization)
        self.assertEqual(row.payload["topic"], "orders_v2")
        self.assertIsNotNone(row.sent_at)
        self.assertIsNotNone(row.received_at)

    def test_repeated_id_is_stored_once(self):
        body = json.dumps(SAMPLE)
        self.client.post(
            "/meli/webhooks/",
            data=body,
            content_type="application/json",
        )
        second = self.client.post(
            "/meli/webhooks/",
            data=body,
            content_type="application/json",
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(MercadoLibreNotification.objects.count(), 1)

    def test_non_json_is_still_accepted(self):
        response = self.client.post(
            "/meli/webhooks/",
            data="not-json",
            content_type="text/plain",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok")
        row = MercadoLibreNotification.objects.get()
        self.assertEqual(row.payload, "not-json")
        self.assertEqual(row.topic, "")
        self.assertEqual(row.notification_id, "")

    def test_known_seller_is_stored_on_their_organization(self):
        organization = Organization.objects.create(name="Vimple")
        MercadoLibreAccount.objects.create(
            organization=organization,
            meli_user_id="468424240",
            access_token="cipher",
            expires_at=timezone.now() + timedelta(hours=1),
        )
        response = self.client.post(
            "/meli/webhooks/",
            data=json.dumps(SAMPLE),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        row = MercadoLibreNotification.objects.get()
        self.assertEqual(row.organization, organization)


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
        page = self.client.get("/organization/")
        self.assertContains(page, "Not connected")
        self.assertNotContains(page, 'href="/meli/connect/"')

    def test_owner_is_sent_to_mercadolibre(self):
        response = self.client.get("/meli/connect/")
        self.assertEqual(response.status_code, 302)
        location = response["Location"]
        self.assertTrue(
            location.startswith("https://auth.mercadolibre.com.ar/authorization?")
        )
        self.assertIn("client_id=app-id", location)
        self.assertIn("state=", location)
        page = self.client.get("/organization/")
        self.assertContains(page, "Connect")

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
        self.assertRedirects(response, "/organization/")
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
        self.assertRedirects(taken, "/organization/?meli=taken")
        self.assertEqual(MercadoLibreAccount.objects.count(), 1)
        self.assertEqual(
            MercadoLibreAccount.objects.get().organization,
            self.organization,
        )
