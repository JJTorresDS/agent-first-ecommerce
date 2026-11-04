import json
from datetime import timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def picker_enabled():
    return bool(settings.GOOGLE_PICKER_API_KEY and settings.GOOGLE_CLOUD_PROJECT_NUMBER)


def _drive_token(user):
    """The user's own Google token that can be refreshed. Only Connect Google Drive stores one."""
    from allauth.socialaccount.models import SocialToken

    if not getattr(user, "is_authenticated", False):
        return None
    return (
        SocialToken.objects.filter(account__user=user, account__provider="google")
        .exclude(token_secret="")
        .order_by("-expires_at")
        .first()
    )


def drive_connected(user):
    return _drive_token(user) is not None


def connect_url(user, next_url):
    """Google sign-in that adds the Drive scope to the account already signed in."""
    auth_params = urlencode(
        {"access_type": "offline", "prompt": "consent", "login_hint": user.email or ""}
    )
    query = urlencode(
        {
            "process": "connect",
            "scope": DRIVE_SCOPE,
            "auth_params": auth_params,
            "next": next_url,
        }
    )
    return f"{reverse('google_login')}?{query}"


def access_token(user):
    """A live access token for this user only. Refreshes it when it is about to expire."""
    token = _drive_token(user)
    if token is None:
        return ""
    if token.expires_at and token.expires_at > timezone.now() + timedelta(seconds=60):
        return token.token
    body = urlencode(
        {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "refresh_token": token.token_secret,
            "grant_type": "refresh_token",
        }
    ).encode()
    request = Request(
        TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode() or "{}")
    fresh = str(payload.get("access_token") or "")
    if not fresh:
        raise ValueError("Google did not return an access token.")
    try:
        seconds = int(payload.get("expires_in") or 3600)
    except (TypeError, ValueError):
        seconds = 3600
    token.token = fresh
    token.expires_at = timezone.now() + timedelta(seconds=seconds)
    token.save(update_fields=["token", "expires_at"])
    return fresh
