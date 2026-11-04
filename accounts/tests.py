from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from allauth.account.signals import user_signed_up
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import SimpleTestCase, TestCase

from accounts.models import (
    AppAccess,
    Invitation,
    Membership,
    Organization,
    Profile,
    assign_default_organization,
    create_owned_organization,
    ensure_profile,
)
from tasks.models import CashflowMovement, Task


User = get_user_model()


class AdminLabelTests(SimpleTestCase):
    def test_vimple_accounts_is_separate_from_allauth(self):
        from django.apps import apps

        config = apps.get_app_config("accounts")
        self.assertEqual(config.label, "accounts")
        self.assertEqual(config.verbose_name, "Vimple accounts")
        self.assertNotEqual(
            config.verbose_name,
            apps.get_app_config("account").verbose_name,
        )


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


def _google_signup(user):
    sociallogin = SimpleNamespace(
        account=SimpleNamespace(provider="google", extra_data={})
    )
    user_signed_up.send(
        sender=user.__class__,
        request=None,
        user=user,
        sociallogin=sociallogin,
    )


class MemberTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("ada", email="ada@example.com")
        self.organization = create_owned_organization(self.owner, "SCENT")
        self.client.force_login(self.owner)

    def test_owner_adds_a_waiting_contributor(self):
        response = self.client.post(
            "/organization/members/",
            {"action": "add", "emails": "ada@gmail.com", "role": "contributor"},
        )
        self.assertRedirects(response, "/organization/members/")
        invitation = Invitation.objects.get()
        self.assertEqual(invitation.organization, self.organization)
        self.assertEqual(invitation.email, "ada@gmail.com")
        self.assertEqual(invitation.role, Membership.Role.CONTRIBUTOR)
        self.assertEqual(mail.outbox, [])
        page = self.client.get("/organization/members/")
        self.assertContains(page, "Gmail account")
        self.assertContains(page, "does not check the address")
        self.assertContains(page, "ada@gmail.com")
        self.assertContains(page, "Waiting for signup")
        self.assertContains(page, "Members")

    def test_an_address_that_is_not_gmail_is_stored(self):
        self.client.post(
            "/organization/members/",
            {"action": "add", "emails": "Ada@Company.com", "role": "admin"},
        )
        invitation = Invitation.objects.get()
        self.assertEqual(invitation.email, "Ada@Company.com")
        self.assertEqual(invitation.role, Membership.Role.ADMIN)

    def test_an_existing_user_joins_this_organization_only(self):
        bea = User.objects.create_user("bea", email="bea@example.com")
        home = assign_default_organization(bea)
        self.client.post(
            "/organization/members/",
            {"action": "add", "emails": "bea@example.com", "role": "contributor"},
        )
        self.assertEqual(Invitation.objects.count(), 0)
        membership = Membership.objects.get(user=bea, organization=self.organization)
        self.assertEqual(membership.role, Membership.Role.CONTRIBUTOR)
        self.assertTrue(Membership.objects.filter(user=bea, organization=home).exists())
        self.assertEqual(mail.outbox, [])

    def test_signup_with_an_invitation_joins_that_organization(self):
        Invitation.objects.create(
            organization=self.organization,
            email="bea@gmail.com",
            role=Membership.Role.CONTRIBUTOR,
        )
        bea = User.objects.create_user("bea", email="bea@gmail.com")
        _google_signup(bea)
        membership = Membership.objects.get(user=bea)
        self.assertEqual(membership.organization, self.organization)
        self.assertEqual(membership.role, Membership.Role.CONTRIBUTOR)
        self.assertEqual(Invitation.objects.count(), 0)
        self.client.force_login(bea)
        setup = self.client.get("/organization/setup/")
        self.assertContains(setup, "Create another organization.")

    def test_signup_without_an_invitation_still_creates_an_organization(self):
        bea = User.objects.create_user("bea", email="bea@gmail.com")
        _google_signup(bea)
        self.assertFalse(Membership.objects.filter(user=bea).exists())
        self.client.force_login(bea)
        response = self.client.post("/organization/setup/", {"name": "Bea Co"})
        self.assertRedirects(response, "/organization/")
        membership = Membership.objects.get(user=bea)
        self.assertEqual(membership.organization.name, "Bea Co")
        self.assertEqual(membership.role, Membership.Role.OWNER)

    def test_only_the_owner_can_delete_the_organization(self):
        self.assertContains(self.client.get("/organization/"), "Delete organization")
        admin = User.objects.create_user("bea", email="bea@example.com")
        Membership.objects.create(
            user=admin,
            organization=self.organization,
            role=Membership.Role.ADMIN,
        )
        contributor = User.objects.create_user("cara", email="cara@example.com")
        Membership.objects.create(
            user=contributor,
            organization=self.organization,
            role=Membership.Role.CONTRIBUTOR,
        )
        for person in (admin, contributor):
            self.client.force_login(person)
            page = self.client.get("/organization/")
            self.assertNotContains(page, "Delete organization")
            denied = self.client.post("/organization/delete/", {"confirm": "yes"})
            self.assertEqual(denied.status_code, 403)
            self.assertTrue(
                Organization.objects.filter(pk=self.organization.id).exists()
            )

    def test_a_contributor_cannot_add_a_member(self):
        bea = User.objects.create_user("bea", email="bea@example.com")
        Membership.objects.create(
            user=bea,
            organization=self.organization,
            role=Membership.Role.CONTRIBUTOR,
        )
        self.client.force_login(bea)
        response = self.client.post(
            "/organization/members/",
            {"action": "add", "emails": "cara@gmail.com", "role": "contributor"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Invitation.objects.count(), 0)
        self.assertNotContains(
            self.client.get("/organization/"),
            'title="Members"',
            status_code=200,
        )

    def test_switching_organization_hides_the_other_members(self):
        Invitation.objects.create(
            organization=self.organization,
            email="secret@gmail.com",
            role=Membership.Role.CONTRIBUTOR,
        )
        other = Organization.objects.create(name="Other")
        Membership.objects.create(
            user=self.owner,
            organization=other,
            role=Membership.Role.OWNER,
        )
        switched = self.client.post(
            "/organization/switch/",
            {"organization": str(other.id)},
        )
        self.assertRedirects(switched, "/organization/")
        page = self.client.get("/organization/members/")
        self.assertContains(page, "Other")
        self.assertNotContains(page, "secret@gmail.com")
        self.client.post("/organization/switch/", {"organization": str(self.organization.id)})
        home = self.client.get("/organization/members/")
        self.assertContains(home, "secret@gmail.com")
        self.assertContains(home, "SCENT")

    def test_a_member_creates_a_second_organization_and_switches_back(self):
        Invitation.objects.create(
            organization=self.organization,
            email="secret@gmail.com",
            role=Membership.Role.CONTRIBUTOR,
        )
        menu = self.client.get("/profile/menu/")
        self.assertContains(menu, 'href="/organization/setup/"')
        page = self.client.get("/organization/setup/")
        self.assertContains(page, "New organization")
        created = self.client.post("/organization/setup/", {"name": "Second"})
        self.assertRedirects(created, "/organization/")
        second = Organization.objects.get(name="Second")
        membership = Membership.objects.get(user=self.owner, organization=second)
        self.assertEqual(membership.role, Membership.Role.OWNER)
        self.assertEqual(Membership.objects.filter(user=self.owner).count(), 2)
        members = self.client.get("/organization/members/")
        self.assertContains(members, "Second")
        self.assertNotContains(members, "secret@gmail.com")
        self.client.post("/organization/switch/", {"organization": str(self.organization.id)})
        home = self.client.get("/organization/members/")
        self.assertContains(home, "secret@gmail.com")

    def test_django_admin_adds_a_member_the_same_way(self):
        admin = User.objects.create_superuser("admin", "admin@example.com", "secret")
        self.client.force_login(admin)
        waiting = self.client.post(
            "/admin/accounts/invitation/add/",
            {
                "organization": str(self.organization.id),
                "email": "cara@gmail.com",
                "role": "contributor",
            },
        )
        self.assertEqual(waiting.status_code, 302)
        self.assertEqual(
            Invitation.objects.get().email,
            "cara@gmail.com",
        )
        person = User.objects.create_user("bea", email="bea@gmail.com")
        linked = self.client.post(
            "/admin/accounts/invitation/add/",
            {
                "organization": str(self.organization.id),
                "email": "bea@gmail.com",
                "role": "admin",
            },
        )
        self.assertRedirects(linked, "/admin/accounts/membership/")
        membership = Membership.objects.get(user=person, organization=self.organization)
        self.assertEqual(membership.role, Membership.Role.ADMIN)
        self.assertFalse(
            Invitation.objects.filter(email__iexact="bea@gmail.com").exists()
        )


class ImpersonationTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser(
            "admin",
            "admin@example.com",
            "secret",
        )
        self.owner = User.objects.create_user("ada", email="ada@example.com")
        self.scent = create_owned_organization(self.owner, "SCENT")
        self.contributor = User.objects.create_user("cara", email="cara@example.com")
        create_owned_organization(self.contributor, "Cara Home")
        Membership.objects.create(
            user=self.contributor,
            organization=self.scent,
            role=Membership.Role.CONTRIBUTOR,
        )
        ensure_profile(self.contributor, self.scent)

    def test_a_superuser_sees_the_contributors_organization(self):
        self.client.force_login(self.superuser)
        row = self.client.get("/admin/auth/user/")
        self.assertContains(row, "Open as this user")
        self.assertContains(
            row,
            f'href="/admin/impersonate/{self.contributor.pk}/"',
        )
        started = self.client.get(f"/admin/impersonate/{self.contributor.pk}/")
        self.assertRedirects(started, "/profile/")
        page = self.client.get("/profile/")
        self.assertContains(page, "Viewing cara@example.com")
        self.assertContains(page, "Exit")
        self.assertContains(page, "SCENT")
        self.assertNotContains(page, 'title="Members"')
        self.assertNotContains(page, "Cara Home")
        self.assertNotContains(page, 'title="Cashflow"')
        self.assertEqual(self.client.get("/cashflow/").status_code, 403)
        self.contributor.profile.refresh_from_db()
        self.assertEqual(self.contributor.profile.organization_id, self.scent.id)
        denied = self.client.get(f"/admin/impersonate/{self.owner.pk}/")
        self.assertContains(
            denied,
            "An impersonation is already active.",
            status_code=403,
        )
        still = self.client.get("/organization/")
        self.assertContains(still, "SCENT")
        self.assertNotContains(still, 'title="Members"')

    def test_exit_returns_the_superuser_to_django_admin(self):
        self.client.force_login(self.superuser)
        self.client.get(f"/admin/impersonate/{self.contributor.pk}/")
        exited = self.client.post("/admin/impersonate/exit/")
        self.assertRedirects(exited, "/admin/")
        self.assertContains(self.client.get("/admin/"), "Vimple")
        setup = self.client.get("/profile/")
        self.assertRedirects(setup, "/organization/setup/")
        page = self.client.get("/organization/setup/")
        self.assertNotContains(page, "Viewing")
        again = self.client.get(f"/admin/impersonate/{self.contributor.pk}/")
        self.assertRedirects(again, "/profile/")

    def test_a_user_who_is_not_a_superuser_cannot_impersonate(self):
        self.client.force_login(self.owner)
        denied = self.client.get(f"/admin/impersonate/{self.contributor.pk}/")
        self.assertContains(
            denied,
            "Only a superuser can open Vimple as another user.",
            status_code=403,
        )
        self.contributor.profile.refresh_from_db()
        self.assertEqual(self.contributor.profile.organization_id, self.scent.id)
