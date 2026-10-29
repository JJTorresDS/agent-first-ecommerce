from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from allauth.account.signals import user_signed_up
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from accounts.models import AppAccess, Profile
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
        Profile.objects.create(
            user=user,
            display_name="Ada Lovelace",
            avatar_url="https://example.com/ada.jpg",
        )
        self.client.force_login(user)

        home = self.client.get("/")
        self.assertRedirects(home, "/profile/")

        response = self.client.get("/profile/")
        self.assertContains(response, "ada@example.com")
        self.assertContains(response, "Ada Lovelace")
        self.assertContains(response, "https://example.com/ada.jpg")
        self.assertNotContains(response, 'name="display_name"')

    def test_google_signup_copies_name_and_picture(self):
        user = User.objects.create_user("ada", email="ada@example.com")
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


def google_signup(user):
    sociallogin = SimpleNamespace(
        account=SimpleNamespace(provider="google", extra_data={})
    )
    user_signed_up.send(
        sender=user.__class__,
        request=None,
        user=user,
        sociallogin=sociallogin,
    )


class SignupTaskTests(TestCase):
    def test_google_signup_assigns_the_task_to_the_superuser(self):
        User.objects.create_superuser("admin", "admin@example.com", "secret")
        user = User.objects.create_user("ada", email="ada@example.com")
        google_signup(user)
        task = Task.objects.get()
        self.assertEqual(task.task_title, "Grant access to ada@example.com")
        self.assertEqual(task.email_address, "ada@example.com")
        self.assertEqual(task.assignee, "admin@example.com")
        self.assertEqual(task.status, Task.Status.TODO)

    def test_google_signup_without_a_superuser_leaves_the_assignee_blank(self):
        user = User.objects.create_user("ada", email="ada@example.com")
        google_signup(user)
        task = Task.objects.get()
        self.assertEqual(task.assignee, "")


class AccessPageTests(TestCase):
    def test_non_superuser_gets_403(self):
        user = User.objects.create_user("ada", email="ada@example.com")
        self.client.force_login(user)
        response = self.client.get("/access/")
        self.assertEqual(response.status_code, 403)

    def test_superuser_grant_lets_the_user_open_cashflow(self):
        admin = User.objects.create_superuser("admin", "admin@example.com", "secret")
        user = User.objects.create_user("ada", email="ada@example.com")
        CashflowMovement.objects.create(
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
