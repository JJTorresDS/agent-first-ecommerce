from types import SimpleNamespace

from allauth.account.signals import user_signed_up
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from accounts.models import Profile


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
