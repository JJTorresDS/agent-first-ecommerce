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
    """Mercado Libre refused the call. message is its own reason, safe to show.

    payload is its whole answer (parsed JSON, or the text when it isn't JSON), for a closer look.
    """

    def __init__(self, message, status=None, payload=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.payload = payload


def describe_failure(error):
    """A log line for a failed call: the error class and Mercado Libre's reason. Never a token."""
    if isinstance(error, MercadoLibreError):
        return f"MercadoLibreError: {error.message}"
    if error.__class__.__name__ == "InvalidToken":
        return "InvalidToken: the saved token was encrypted with another SECRET_KEY"
    return error.__class__.__name__


def _error_payload(error):
    """Mercado Libre's answer to a refused call: parsed JSON, else up to 4,000 characters of text."""
    try:
        text = error.read().decode(errors="replace")
    except OSError:
        return None
    try:
        return json.loads(text or "null")
    except ValueError:
        return text[:4000]


def _error_reason(error, payload=None):
    reason = ""
    if isinstance(payload, dict):
        causes = payload.get("cause")
        if isinstance(causes, list):
            # An error is the reason; a warning only when there is nothing else.
            messages = [cause for cause in causes if isinstance(cause, dict) and cause.get("message")]
            errors = [cause for cause in messages if cause.get("type") != "warning"]
            if errors or messages:
                reason = str((errors or messages)[0]["message"])
        reason = reason or str(payload.get("message") or payload.get("error") or "")
    reason = " ".join(reason.split())[:300]
    return reason or f"HTTP {error.code}"


def _api_request(access_token, url, payload=None, expect=dict, method=None):
    """GET, or POST when there is a payload; method overrides it (DELETE, PUT)."""
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
    }
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=15) as response:
            parsed = json.loads(response.read().decode() or "null")
    except HTTPError as error:
        payload = _error_payload(error)
        raise MercadoLibreError(_error_reason(error, payload), error.code, payload) from None
    if not isinstance(parsed, expect):
        names = " or ".join(kind.__name__ for kind in (expect if isinstance(expect, tuple) else (expect,)))
        raise ValueError(f"Mercado Libre response was not a {names}.")
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


LISTING_STATUSES = ("active", "paused", "closed", "under_review", "inactive")
ITEMS_PER_CALL = 20


def fetch_seller_item_ids(access_token, seller_id, status):
    """Every item id of one status. The scan search pages with scroll_id past 1,000 items."""
    seller = quote(str(seller_id), safe="")
    ids = []
    scroll_id = ""
    for _page in range(2000):
        fields = {"status": status, "search_type": "scan", "limit": 100}
        if scroll_id:
            fields["scroll_id"] = scroll_id
        payload = _api_request(
            access_token,
            f"https://api.mercadolibre.com/users/{seller}/items/search?{urlencode(fields)}",
        )
        results = payload.get("results")
        if not isinstance(results, list):
            raise ValueError("Mercado Libre item search had no results.")
        if not results:
            break
        ids.extend(str(item) for item in results if item)
        scroll_id = str(payload.get("scroll_id") or "")
        if not scroll_id:
            break
    return ids


def fetch_items(access_token, item_ids):
    """Items by id, 20 per call, with every attribute (variation SKUs come only this way).

    Returns {item_id: body}, and {item_id: None} for an item Mercado Libre no longer has.
    """
    found = {}
    item_ids = list(item_ids)
    for start in range(0, len(item_ids), ITEMS_PER_CALL):
        chunk = item_ids[start : start + ITEMS_PER_CALL]
        query = urlencode({"ids": ",".join(chunk), "include_attributes": "all"})
        rows = _api_request(
            access_token,
            f"https://api.mercadolibre.com/items?{query}",
            expect=list,
        )
        if len(rows) != len(chunk):
            raise ValueError("Mercado Libre returned a different number of items than asked for.")
        # Rows come back in the order the ids were asked for, even for an item that is gone.
        for item_id, row in zip(chunk, rows):
            row = row if isinstance(row, dict) else {}
            body = row.get("body") if isinstance(row.get("body"), dict) else {}
            if row.get("code") == 200 and body.get("id"):
                found[str(body["id"])] = body
            elif row.get("code") == 404:
                found[item_id] = None
            else:
                reason = " ".join(str(body.get("message") or body.get("error") or "").split())[:300]
                raise MercadoLibreError(reason or f"HTTP {row.get('code')}", row.get("code"))
    return found


def fetch_full_stock(access_token, inventory_id):
    """Units Mercado Libre holds in Full for one inventory id."""
    inventory = quote(str(inventory_id), safe="")
    return _api_request(
        access_token,
        f"https://api.mercadolibre.com/inventories/{inventory}/stock/fulfillment",
    )


ORDER_HISTORY_LIMIT = 5000


def fetch_orders_since(access_token, seller_id, since):
    """Every order created since a moment, oldest first, up to 5,000."""
    orders = []
    offset = 0
    while offset < ORDER_HISTORY_LIMIT:
        query = urlencode(
            {
                "seller": seller_id,
                "order.date_created.from": _iso(since),
                "sort": "date_asc",
                "limit": 50,
                "offset": offset,
            }
        )
        payload = _api_request(access_token, f"https://api.mercadolibre.com/orders/search?{query}")
        batch = payload.get("results")
        if not isinstance(batch, list):
            raise ValueError("Mercado Libre response had no orders.")
        orders.extend(order for order in batch if isinstance(order, dict))
        paging = payload.get("paging") if isinstance(payload.get("paging"), dict) else {}
        total = paging.get("total")
        offset += 50
        if len(batch) < 50 or (isinstance(total, int) and offset >= total):
            break
    return orders


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


def fetch_catalog_product(access_token, product_id):
    """A catalog product (the /p/MLA… page): name, status, domain_id, pictures."""
    product = quote(str(product_id), safe="")
    return _api_request(access_token, f"https://api.mercadolibre.com/products/{product}")


def predict_category(access_token, site_id, text):
    """Mercado Libre's best guesses for a title: [{domain_id, category_id, category_name}, …]."""
    site = quote(str(site_id), safe="")
    query = urlencode({"q": text, "limit": 5})
    return _api_request(
        access_token,
        f"https://api.mercadolibre.com/sites/{site}/domain_discovery/search?{query}",
        expect=list,
    )


def fetch_category_attributes(access_token, category_id):
    category = quote(str(category_id), safe="")
    return _api_request(
        access_token,
        f"https://api.mercadolibre.com/categories/{category}/attributes",
        expect=list,
    )


def only_warnings(error):
    """True when Mercado Libre's refusal lists causes and every one is a warning, which doesn't block a listing."""
    payload = getattr(error, "payload", None)
    causes = payload.get("cause") if isinstance(payload, dict) else None
    if not isinstance(causes, list) or not causes:
        return False
    return all(isinstance(cause, dict) and cause.get("type") == "warning" for cause in causes)


def validate_item(access_token, body):
    """Mercado Libre's check of a new item, without creating it. Raises MercadoLibreError with its reason."""
    _api_request(access_token, "https://api.mercadolibre.com/items/validate", body, expect=(dict, type(None)))


def create_item(access_token, body):
    return _api_request(access_token, "https://api.mercadolibre.com/items", body)


def remove_flex(access_token, site_id, item_id):
    """Take one item out of Mercado Envíos Flex (the account puts new items in)."""
    site = quote(str(site_id), safe="")
    item = quote(str(item_id), safe="")
    _api_request(
        access_token,
        f"https://api.mercadolibre.com/sites/{site}/shipping/selfservice/items/{item}",
        method="DELETE",
        expect=(dict, list, type(None)),
    )
