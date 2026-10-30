from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from allauth.account.signals import user_signed_up
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from accounts.models import (
    AppAccess,
    Membership,
    Organization,
    Profile,
    assign_default_organization,
)
from tasks.models import CashflowMovement, Task


User = get_user_model()


class AnonymousAccessTests(SimpleTestCase):
    def test_home_sends_visitors_to_google(self):
        response = self.client.get("/")
        self.assertRedirects(
            response,
            "/accounts/google/login/",
            fetch_redirect_response=False,
        )

    def test_profile_sends_visitors_to_google(self):
        response = self.client.get("/profile/")
        self.assertRedirects(
            response,
            "/accounts/google/login/?next=/profile/",
            fetch_redirect_response=False,
        )

    def test_cashflow_and_access_send_visitors_to_google(self):
        cashflow = self.client.get("/cashflow/")
        self.assertRedirects(
            cashflow,
            "/accounts/google/login/?next=/cashflow/",
            fetch_redirect_response=False,
        )
        access = self.client.get("/access/")
        self.assertRedirects(
            access,
            "/accounts/google/login/?next=/access/",
            fetch_redirect_response=False,
        )

    def test_password_signup_is_closed(self):
        response = self.client.get("/accounts/signup/")
        self.assertEqual(response.status_code, 404)


class ProfileAccessTests(TestCase):
    def test_google_login_page_asks_to_continue(self):
        response = self.client.get("/accounts/google/login/")
        self.assertContains(response, "Sign in with Google")
        self.assertContains(response, "Continue")

    def test_signed_in_user_sees_profile(self):
        user = User.objects.create_user(
            "ada",
            email="ada@example.com",
            password="not-used-for-google",
        )
        assign_default_organization(user)
        profile = Profile.objects.get(user=user)
        profile.display_name = "Ada Lovelace"
        profile.avatar_url = "https://example.com/ada.jpg"
        profile.save()
        self.client.force_login(user)

        home = self.client.get("/")
        self.assertRedirects(home, "/profile/")

        response = self.client.get("/profile/")
        self.assertContains(response, "ada@example.com")
        self.assertContains(response, "Ada Lovelace")
        self.assertContains(response, "https://example.com/ada.jpg")
        self.assertNotContains(response, 'name="display_name"')
        self.assertContains(response, 'nav-label">Administration')
        self.assertNotContains(response, 'nav-label">Account')
        self.assertContains(response, 'hx-get="/profile/menu/"')
        menu = self.client.get("/profile/menu/")
        self.assertContains(menu, "Ada Lovelace")
        self.assertContains(menu, "ada@example.com")
        self.assertContains(menu, "Organization")
        self.assertContains(menu, "Profile")
        self.assertContains(menu, "default")
        self.assertContains(menu, 'href="/profile/"')
        self.assertContains(menu, "Log out")

    def test_google_signup_copies_name_and_picture(self):
        user = User.objects.create_user("ada", email="ada@example.com")
        assign_default_organization(user)
        sociallogin = SimpleNamespace(
            account=SimpleNamespace(
                provider="google",
                extra_data={
                    "name": "Ada Lovelace",
                    "picture": "https://example.com/ada.jpg",
                },
            )
        )
        user_signed_up.send(
            sender=user.__class__,
            request=None,
            user=user,
            sociallogin=sociallogin,
        )
        profile = Profile.objects.get(user=user)
        self.assertEqual(profile.display_name, "Ada Lovelace")
        self.assertEqual(profile.avatar_url, "https://example.com/ada.jpg")


class SignupTaskTests(TestCase):
    def test_default_organization_assigns_the_task_to_the_superuser(self):
        User.objects.create_superuser("admin", "admin@example.com", "secret")
        user = User.objects.create_user("ada", email="ada@example.com")
        organization = assign_default_organization(user)
        task = Task.objects.get()
        self.assertEqual(task.organization, organization)
        self.assertEqual(task.task_title, "Grant access to ada@example.com")
        self.assertEqual(task.email_address, "ada@example.com")
        self.assertEqual(task.assignee, "admin@example.com")
        self.assertEqual(task.status, Task.Status.TODO)

    def test_organization_without_a_superuser_leaves_the_assignee_blank(self):
        user = User.objects.create_user("ada", email="ada@example.com")
        organization = assign_default_organization(user)
        task = Task.objects.get()
        self.assertEqual(task.organization, organization)
        self.assertEqual(task.assignee, "")


class AccessPageTests(TestCase):
    def test_non_superuser_gets_403(self):
        user = User.objects.create_user("ada", email="ada@example.com")
        assign_default_organization(user)
        self.client.force_login(user)
        response = self.client.get("/access/")
        self.assertEqual(response.status_code, 403)

    def test_superuser_grant_lets_the_user_open_cashflow(self):
        admin = User.objects.create_superuser("admin", "admin@example.com", "secret")
        user = User.objects.create_user("ada", email="ada@example.com")
        assign_default_organization(admin)
        user_org = assign_default_organization(user)
        CashflowMovement.objects.create(
            organization=user_org,
            uploader_email="ada@example.com",
            transaction_date=date(2026, 10, 7),
            category="compra",
            amount=Decimal("-58000.50"),
            currency="ARS",
            description="Secret purchase",
        )
        self.client.force_login(user)
        waiting = self.client.get("/cashflow/")
        self.assertContains(waiting, "Waiting for access", status_code=403)
        self.assertNotContains(waiting, "-58000.50", status_code=403)
        self.assertNotContains(waiting, "Secret purchase", status_code=403)

        self.client.force_login(admin)
        tasks_grant = self.client.post(
            f"/access/{user.pk}/",
            {"app": "tasks", "enabled": "on"},
        )
        self.assertRedirects(tasks_grant, "/access/")
        cashflow_grant = self.client.post(
            f"/access/{user.pk}/",
            {"app": "cashflow", "enabled": "on"},
        )
        self.assertRedirects(cashflow_grant, "/access/")
        self.assertTrue(
            AppAccess.objects.filter(user=user, app=AppAccess.App.TASKS).exists()
        )
        self.assertTrue(
            AppAccess.objects.filter(user=user, app=AppAccess.App.CASHFLOW).exists()
        )

        self.client.force_login(user)
        opened = self.client.get("/cashflow/")
        self.assertContains(opened, "Secret purchase")
        self.assertContains(opened, "-58000.50")


class OrganizationTests(TestCase):
    def test_setup_names_the_organization_and_makes_the_user_owner(self):
        user = User.objects.create_user("ada", email="ada@example.com")
        self.client.force_login(user)
        response = self.client.post("/organization/setup/", {"name": "  Vimple  "})
        self.assertRedirects(response, "/organization/")
        membership = Membership.objects.get(user=user)
        self.assertEqual(membership.organization.name, "Vimple")
        self.assertEqual(membership.role, Membership.Role.OWNER)
        self.assertIsNotNone(membership.organization.id)
        task = Task.objects.get()
        self.assertEqual(task.organization, membership.organization)
        self.assertEqual(task.task_title, "Grant access to ada@example.com")

    def test_blank_name_is_rejected(self):
        user = User.objects.create_user("ada", email="ada@example.com")
        self.client.force_login(user)
        response = self.client.post("/organization/setup/", {"name": "   "})
        self.assertContains(response, "Enter an organization name.")
        self.assertEqual(Organization.objects.count(), 0)

    def test_backfill_names_a_missing_organization_default(self):
        user = User.objects.create_user("ada", email="ada@example.com")
        organization = assign_default_organization(user)
        self.assertEqual(organization.name, "default")
        self.assertEqual(assign_default_organization(user).id, organization.id)
        self.assertEqual(
            Membership.objects.get(user=user).role,
            Membership.Role.OWNER,
        )

    def test_rename_keeps_the_same_id(self):
        user = User.objects.create_user("ada", email="ada@example.com")
        self.client.force_login(user)
        self.client.post("/organization/setup/", {"name": "Vimple"})
        organization_id = Membership.objects.get(user=user).organization_id
        response = self.client.post("/organization/", {"name": "Scent"})
        self.assertRedirects(response, "/organization/")
        organization = Organization.objects.get(pk=organization_id)
        self.assertEqual(organization.name, "Scent")
        self.assertEqual(organization.id, organization_id)
        page = self.client.get("/organization/")
        self.assertContains(page, "Your organization")
        self.assertContains(page, str(organization_id))
        self.assertContains(page, "Active members")
        self.assertContains(page, 'nav-label">Administration')
        self.assertNotContains(page, 'nav-label">Account')
        self.assertContains(page, 'class="nav-link active" title="Organization" href="/organization/"')
        self.assertNotContains(page, 'title="Profile"')
        self.assertContains(page, ">AE<")

    def test_profile_without_an_organization_goes_to_setup(self):
        user = User.objects.create_user("ada", email="ada@example.com")
        self.client.force_login(user)
        response = self.client.get("/profile/")
        self.assertRedirects(response, "/organization/setup/")

    def test_delete_removes_the_organization_and_the_next_id_is_new(self):
        user = User.objects.create_user("ada", email="ada@example.com")
        self.client.force_login(user)
        self.client.post("/organization/setup/", {"name": "Vimple"})
        organization_id = Membership.objects.get(user=user).organization_id
        kept = self.client.post("/organization/delete/", {})
        self.assertContains(kept, "Confirm the deletion")
        self.assertTrue(Organization.objects.filter(pk=organization_id).exists())
        deleted = self.client.post("/organization/delete/", {"confirm": "yes"})
        self.assertRedirects(deleted, "/organization/setup/")
        self.assertFalse(Organization.objects.filter(pk=organization_id).exists())
        self.client.post("/organization/setup/", {"name": "Next"})
        self.assertNotEqual(
            Membership.objects.get(user=user).organization_id,
            organization_id,
        )
