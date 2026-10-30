import json
from datetime import timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.utils import timezone


def expires_at_from(expires_in):
    try:
        seconds = int(expires_in)
    except (TypeError, ValueError):
        seconds = 0
    return timezone.now() + timedelta(seconds=seconds)


def _token_request(fields):
    body = urlencode(fields).encode()
    request = Request(
        settings.MELI_TOKEN_URL,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode())


def exchange_code(code, redirect_uri):
    return _token_request(
        {
            "grant_type": "authorization_code",
            "client_id": settings.MELI_CLIENT_ID,
            "client_secret": settings.MELI_CLIENT_SECRET,
            "code": code,
            "redirect_uri": redirect_uri,
        }
    )


def refresh_access_token(refresh_token):
    return _token_request(
        {
            "grant_type": "refresh_token",
            "client_id": settings.MELI_CLIENT_ID,
            "client_secret": settings.MELI_CLIENT_SECRET,
            "refresh_token": refresh_token,
        }
    )
