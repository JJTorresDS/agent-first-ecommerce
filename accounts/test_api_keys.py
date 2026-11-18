from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings

from accounts.models import Membership, Organization, OrganizationApiKey
from catalog.test_views import member
from meli.client import MercadoLibreError
from meli.tests import shared_seller

URL = "/api/meli/token"


class ApiKeyPageTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Seller")
        self.owner = member(self.organization)
        self.client.force_login(self.owner)

    def test_an_owner_creates_sees_it_masked_and_copies_it(self):
        page = self.client.get("/organization/")
        self.assertContains(page, "Create API key")
        self.assertRedirects(self.client.post("/organization/api-key/", {"action": "create"}), "/organization/")
        key = OrganizationApiKey.objects.get(organization=self.organization)
        raw = key.value()
        self.assertTrue(raw.startswith("vk_"))
        page = self.client.get("/organization/")
        self.assertContains(page, key.masked())
        self.assertNotContains(page, raw)
        self.assertNotContains(page, key.key_encrypted)
        copied = self.client.post("/organization/api-key/copy/")
        self.assertEqual(copied.content.decode(), raw)
        self.assertEqual(copied["Cache-Control"], "no-store")
        self.assertEqual(self.client.get("/organization/api-key/copy/").status_code, 405)

    def test_a_new_key_shows_a_curl_command_with_the_key_masked(self):
        raw = OrganizationApiKey.generate(self.organization, self.owner)
        key = OrganizationApiKey.objects.get()
        page = self.client.get("/organization/")
        self.assertContains(page, "Use it with curl")
        self.assertContains(page, '<details class="api-curl" open>')
        self.assertContains(page, f'Authorization: Bearer {key.masked()}')
        self.assertContains(page, f"http://testserver/api/meli/token?organization={self.organization.pk}")
        self.assertContains(page, 'id="copy-api-curl"')
        self.assertNotContains(page, raw)
        OrganizationApiKey.objects.update(last_used_at=key.created_at)
        self.assertContains(self.client.get("/organization/"), '<details class="api-curl">')

    def test_regenerate_replaces_and_delete_removes_one_key_per_organization(self):
        first = OrganizationApiKey.generate(self.organization, self.owner)
        self.client.post("/organization/api-key/", {"action": "regenerate"})
        self.assertEqual(OrganizationApiKey.objects.filter(organization=self.organization).count(), 1)
        self.assertIsNone(OrganizationApiKey.matching(first))
        self.client.post("/organization/api-key/", {"action": "delete"})
        self.assertFalse(OrganizationApiKey.objects.exists())
        self.assertEqual(self.client.post("/organization/api-key/copy/").status_code, 404)

    def test_admins_and_members_can_neither_see_nor_change_it(self):
        OrganizationApiKey.generate(self.organization, self.owner)
        for role, username in ((Membership.Role.ADMIN, "admin"), (Membership.Role.CONTRIBUTOR, "mem")):
            self.client.force_login(member(self.organization, role=role, username=username))
            page = self.client.get("/organization/")
            self.assertNotContains(page, 'id="api-key"')
            for action in ("create", "regenerate", "delete"):
                self.assertEqual(self.client.post("/organization/api-key/", {"action": action}).status_code, 403)
            self.assertEqual(self.client.post("/organization/api-key/copy/").status_code, 403)
        self.assertEqual(OrganizationApiKey.objects.count(), 1)

    def test_a_key_encrypted_elsewhere_offers_regenerate_and_still_authenticates(self):
        raw = OrganizationApiKey.generate(self.organization, self.owner)
        OrganizationApiKey.objects.update(key_encrypted="gAAAAA-not-ours")
        page = self.client.get("/organization/")
        self.assertContains(page, "Regenerate it to get one you can copy")
        self.assertNotContains(page, 'id="copy-api-key"')
        self.assertNotContains(page, 'id="copy-api-curl"')
        self.assertEqual(OrganizationApiKey.matching(raw).organization, self.organization)


class TokenApiTests(TestCase):
    def setUp(self):
        self.organization = shared_seller("Test Seller")
        self.owner = member(self.organization)
        self.raw = OrganizationApiKey.generate(self.organization, self.owner)

    def get(self, organization=None, key=None):
        return self.client.get(
            URL, {"organization": str(organization or self.organization.pk)},
            HTTP_AUTHORIZATION=f"Bearer {key or self.raw}",
        )

    def test_the_key_gets_its_organizations_token_and_never_the_refresh_token(self):
        response = self.get()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["access_token"], "access-1")
        self.assertEqual(body["token_type"], "Bearer")
        self.assertEqual(body["meli_user_id"], "468424240")
        self.assertNotIn("refresh-1", response.content.decode())
        self.assertIsNotNone(OrganizationApiKey.objects.get().last_used_at)

    def test_no_wrong_or_old_key_is_refused(self):
        self.assertEqual(self.client.get(URL, {"organization": str(self.organization.pk)}).status_code, 401)
        self.assertEqual(self.get(key="vk_nope").status_code, 401)
        self.assertEqual(self.get(key=str(self.organization.pk)).status_code, 401)
        OrganizationApiKey.generate(self.organization, self.owner)
        self.assertEqual(self.get().status_code, 401)

    def test_a_key_never_reaches_another_organization(self):
        other = shared_seller("Other", user_id="555")
        theirs = OrganizationApiKey.generate(other, member(other, username="bea"))
        self.assertEqual(self.get(organization=other.pk).status_code, 404)
        self.assertEqual(self.get(key=theirs).status_code, 404)
        self.assertEqual(self.get(organization="5d1b7c2e-0000-4000-8000-000000000000").status_code, 404)
        self.assertEqual(self.get(organization="not-a-uuid").status_code, 422)

    def test_not_connected(self):
        lone = Organization.objects.create(name="Lone")
        key = OrganizationApiKey.generate(lone, member(lone, username="cy"))
        response = self.get(organization=lone.pk, key=key)
        self.assertEqual(response.status_code, 404)
        self.assertIn("isn't connected", response.json()["detail"])

    @override_settings(MELI_CLIENT_ID="1", MELI_CLIENT_SECRET="s")
    def test_an_expired_token_is_refreshed_by_vimple(self):
        self.organization.mercadolibre_account.__class__.objects.update(
            expires_at=self.organization.created_at - timedelta(hours=1)
        )
        answer = {"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 21600, "user_id": 468424240}
        with patch("meli.client.refresh_access_token", return_value=answer) as refresh:
            response = self.get()
        refresh.assert_called_once_with("refresh-1")
        self.assertEqual(response.json()["access_token"], "access-2")
        self.assertNotIn("refresh-2", response.content.decode())

    def test_a_refused_refresh_answers_503_without_tokens(self):
        self.organization.mercadolibre_account.__class__.objects.update(
            expires_at=self.organization.created_at - timedelta(hours=1)
        )
        with patch("meli.client.refresh_access_token", side_effect=MercadoLibreError("invalid_grant")):
            response = self.get()
        self.assertEqual(response.status_code, 503)
        self.assertIn("invalid_grant", response.json()["detail"])
        self.assertNotIn("refresh-1", response.content.decode())
