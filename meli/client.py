import json
from datetime import timedelta
from urllib.parse import quote, urlencode
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


def _api_request(access_token, url, payload=None):
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
    }
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers)
    with urlopen(request, timeout=15) as response:
        parsed = json.loads(response.read().decode() or "null")
    if not isinstance(parsed, dict):
        raise ValueError("Mercado Libre response was not an object.")
    return parsed


def fetch_user_profile(access_token):
    return _api_request(access_token, "https://api.mercadolibre.com/users/me")


def fetch_question(access_token, question_id):
    question = quote(str(question_id), safe="")
    return _api_request(
        access_token,
        f"https://api.mercadolibre.com/questions/{question}",
    )


def fetch_item(access_token, item_id):
    item = quote(str(item_id), safe="")
    return _api_request(access_token, f"https://api.mercadolibre.com/items/{item}")


def post_answer(access_token, question_id, text):
    return _api_request(
        access_token,
        "https://api.mercadolibre.com/answers",
        {"question_id": int(question_id), "text": text},
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
