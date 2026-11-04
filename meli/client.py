import json
from datetime import timedelta, timezone as dt_timezone
from urllib.error import HTTPError
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


class MercadoLibreError(ValueError):
    """Mercado Libre refused the call. message is its own reason, safe to show."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


def describe_failure(error):
    """A log line for a failed call: the error class and Mercado Libre's reason. Never a token."""
    if isinstance(error, MercadoLibreError):
        return f"MercadoLibreError: {error.message}"
    if error.__class__.__name__ == "InvalidToken":
        return "InvalidToken: the saved token was encrypted with another SECRET_KEY"
    return error.__class__.__name__


def _error_reason(error):
    try:
        payload = json.loads(error.read().decode() or "null")
    except (OSError, ValueError):
        payload = None
    reason = ""
    if isinstance(payload, dict):
        causes = payload.get("cause")
        if isinstance(causes, list):
            for cause in causes:
                if isinstance(cause, dict) and cause.get("message"):
                    reason = str(cause["message"])
                    break
        reason = reason or str(payload.get("message") or payload.get("error") or "")
    reason = " ".join(reason.split())[:300]
    return reason or f"HTTP {error.code}"


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
    try:
        with urlopen(request, timeout=15) as response:
            parsed = json.loads(response.read().decode() or "null")
    except HTTPError as error:
        raise MercadoLibreError(_error_reason(error)) from None
    if not isinstance(parsed, dict):
        raise ValueError("Mercado Libre response was not an object.")
    return parsed


def fetch_user_profile(access_token):
    return _api_request(access_token, "https://api.mercadolibre.com/users/me")


def fetch_unanswered_questions(access_token, seller_id):
    query = urlencode(
        {
            "seller_id": seller_id,
            "status": "UNANSWERED",
            "limit": 50,
        }
    )
    return _api_request(
        access_token,
        f"https://api.mercadolibre.com/questions/search?{query}",
    )


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


def _user_id(value):
    text = str(value)
    if text.isdigit():
        return int(text)
    return text


def fetch_message(access_token, message_id):
    """One post-sale message. Does not mark it read."""
    message = quote(str(message_id), safe="")
    query = urlencode({"tag": "post_sale", "mark_as_read": "false"})
    return _api_request(
        access_token,
        f"https://api.mercadolibre.com/messages/{message}?{query}",
    )


def fetch_pack_messages(access_token, pack_id, seller_id):
    """The full post-sale thread. Does not mark the messages read."""
    pack = quote(str(pack_id), safe="")
    seller = quote(str(seller_id), safe="")
    messages = []
    status = {}
    offset = 0
    while offset < 200:
        query = urlencode(
            {
                "tag": "post_sale",
                "mark_as_read": "false",
                "limit": 50,
                "offset": offset,
            }
        )
        payload = _api_request(
            access_token,
            f"https://api.mercadolibre.com/messages/packs/{pack}/sellers/{seller}?{query}",
        )
        batch = payload.get("messages")
        if not isinstance(batch, list):
            raise ValueError("Mercado Libre response had no messages.")
        if not status and isinstance(payload.get("conversation_status"), dict):
            status = payload["conversation_status"]
        messages.extend(item for item in batch if isinstance(item, dict))
        paging = payload.get("paging") if isinstance(payload.get("paging"), dict) else {}
        total = paging.get("total")
        offset += 50
        if not batch or (isinstance(total, int) and offset >= total):
            break
    return {"messages": messages, "conversation_status": status}


def fetch_unread_post_sale(access_token):
    query = urlencode({"tag": "post_sale", "role": "seller"})
    return _api_request(
        access_token,
        f"https://api.mercadolibre.com/messages/unread?{query}",
    )


def fetch_recent_orders(access_token, seller_id, since, until):
    """Orders in the window, newest first, capped at 150."""
    orders = []
    offset = 0
    while offset < 150:
        query = urlencode(
            {
                "seller": seller_id,
                "order.date_created.from": _iso(since),
                "order.date_created.to": _iso(until),
                "sort": "date_desc",
                "limit": 50,
                "offset": offset,
            }
        )
        payload = _api_request(
            access_token,
            f"https://api.mercadolibre.com/orders/search?{query}",
        )
        batch = payload.get("results")
        if not isinstance(batch, list):
            raise ValueError("Mercado Libre response had no orders.")
        orders.extend(item for item in batch if isinstance(item, dict))
        paging = payload.get("paging") if isinstance(payload.get("paging"), dict) else {}
        total = paging.get("total")
        offset += 50
        if len(batch) < 50 or (isinstance(total, int) and offset >= total):
            break
    return orders[:150]


def fetch_order(access_token, order_id):
    order = quote(str(order_id), safe="")
    return _api_request(access_token, f"https://api.mercadolibre.com/orders/{order}")


def fetch_pack(access_token, pack_id):
    pack = quote(str(pack_id), safe="")
    return _api_request(access_token, f"https://api.mercadolibre.com/packs/{pack}")


def post_pack_message(access_token, pack_id, seller_id, buyer_id, text):
    pack = quote(str(pack_id), safe="")
    seller = quote(str(seller_id), safe="")
    query = urlencode({"tag": "post_sale"})
    return _api_request(
        access_token,
        f"https://api.mercadolibre.com/messages/packs/{pack}/sellers/{seller}?{query}",
        {
            "from": {"user_id": _user_id(seller_id)},
            "to": {"user_id": _user_id(buyer_id)},
            "text": text,
        },
    )


def _iso(value):
    return value.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def refresh_access_token(refresh_token):
    return _token_request(
        {
            "grant_type": "refresh_token",
            "client_id": settings.MELI_CLIENT_ID,
            "client_secret": settings.MELI_CLIENT_SECRET,
            "refresh_token": refresh_token,
        }
    )
