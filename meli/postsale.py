import logging
import re
from datetime import datetime, timedelta, timezone as dt_timezone

from cryptography.fernet import InvalidToken
from django.utils import timezone

from inbox.models import Channel, Conversation, Message
from inbox.services import upsert_thread

from .client import (
    describe_failure,
    fetch_message,
    fetch_order,
    fetch_pack,
    fetch_pack_messages,
    fetch_recent_orders,
    fetch_unread_post_sale,
)
from .models import MercadoLibreAccount, MercadoLibreNotification, SellerProfile

logger = logging.getLogger(__name__)

CHANNEL = Channel.MELI_POSTSALES
WINDOW = timedelta(days=7)
_OLDEST = datetime.min.replace(tzinfo=dt_timezone.utc)
_PACK_RESOURCE = re.compile(r"/packs/([^/]+)")
_MESSAGE_RESOURCE = re.compile(r"/messages/([^/]+)")
# Mercado Libre's messages webhook sends the bare message id as the resource.
_BARE_MESSAGE = re.compile(r"[A-Za-z0-9_-]{8,128}")
CATCH_UP_WINDOW = timedelta(days=2)
CATCH_UP_LIMIT = 20
# The seller's sale page. The messages page would mark the buyer's message read.
SITE_DOMAINS = {
    "MLA": "com.ar",
    "MLM": "com.mx",
    "MLB": "com.br",
    "MLC": "cl",
    "MCO": "com.co",
    "MLU": "com.uy",
    "MPE": "com.pe",
}


def project_pack(organization, seller_id, pack_id, thread, order=None):
    """Write one pack thread into this organization's inbox."""
    pack_id = _ident(pack_id)
    if not pack_id or organization is None:
        return None
    seller_id = _ident(seller_id)
    bubbles = _bubbles(thread.get("messages") or [], seller_id)
    if not bubbles:
        return None
    bubbles.sort(key=lambda item: item["sent_at"] or _OLDEST)
    latest = bubbles[-1]
    active = _is_active(thread.get("conversation_status"))
    needs_reply = active and latest["direction"] == Message.Direction.INBOUND
    title = _sale_title(order) or pack_id
    link = _link(order) or _sale_link(organization, pack_id)
    existing = Conversation.objects.filter(
        organization=organization,
        channel=CHANNEL,
        external_id=pack_id,
    ).first()
    if existing is not None:
        if title == pack_id and existing.title:
            title = existing.title
        if not link:
            link = existing.link_url
    return upsert_thread(
        organization=organization,
        channel=CHANNEL,
        external_id=pack_id,
        title=title,
        preview=latest["body"],
        link_url=link,
        needs_reply=needs_reply,
        last_message_at=latest["sent_at"],
        messages=bubbles,
        counterpart_id=buyer_id(thread, seller_id),
    )


def load_pack(organization, pack_id, order=None):
    pack_id = _ident(pack_id)
    if not pack_id:
        return None
    account = _account_for(organization)
    if account is None:
        return None
    token = account.fresh_access_token()
    seller_id = _ident(account.meli_user_id)
    thread = fetch_pack_messages(token, pack_id, seller_id)
    if order is None and not _titled(organization, pack_id):
        order = _order_for_pack(token, pack_id)
    return project_pack(organization, seller_id, pack_id, thread, order)


def _titled(organization, pack_id):
    """The chat already has its sale title and link, so the order needn't be fetched again."""
    return (
        Conversation.objects.filter(organization=organization, channel=CHANNEL, external_id=pack_id)
        .exclude(title=pack_id)
        .exclude(title="")
        .exclude(link_url="")
        .exists()
    )


def load_postsale_from_notification(parsed):
    from .views import _clip, _organizations_for

    if _clip(parsed.get("topic"), 64) != "messages":
        return []
    loaded = []
    for organization in _organizations_for(_clip(parsed.get("user_id"), 64)):
        try:
            conversation = _load_from_notification(organization, parsed)
        except (OSError, ValueError, InvalidToken) as error:
            logger.warning(
                "Post-sale message %s could not be loaded for %s: %s",
                parsed.get("resource"),
                organization.pk,
                describe_failure(error),
            )
            continue
        if conversation is not None:
            loaded.append(conversation)
    return loaded


def _load_from_notification(organization, parsed):
    account = _account_for(organization)
    if account is None:
        return None
    resource = str(parsed.get("resource") or "")
    pack_id = _pack_id(resource)
    token = None
    if not pack_id:
        message_id = _message_id(resource)
        if not message_id:
            return None
        token = account.fresh_access_token()
        payload = fetch_message(token, message_id)
        pack_id = _pack_from_message(_message_in(payload, message_id))
        if not pack_id:
            logger.warning(
                "Post-sale message %s for %s names no pack or order (response keys: %s)",
                message_id,
                organization.pk,
                ", ".join(sorted(payload)) if isinstance(payload, dict) else type(payload).__name__,
            )
    if not pack_id:
        return None
    return load_pack(organization, pack_id)


def _message_in(payload, message_id):
    """The message itself. GET /messages/<id> wraps it in a messages list like a thread."""
    if not isinstance(payload, dict):
        return {}
    listed = payload.get("messages")
    if not isinstance(listed, list):
        return payload
    found = [item for item in listed if isinstance(item, dict)]
    for item in found:
        if _ident(item.get("id")) == _ident(message_id):
            return item
    return found[0] if found else {}


def refresh_postsale(organization):
    """Project stored message notifications that never became a chat."""
    if organization is None:
        return
    notifications = list(
        MercadoLibreNotification.objects.filter(
            organization=organization,
            topic="messages",
            created_at__gte=timezone.now() - CATCH_UP_WINDOW,
        ).order_by("-created_at")[:CATCH_UP_LIMIT]
    )
    if not notifications:
        return
    account = _account_for(organization)
    if account is None:
        return
    known_packs = set(
        Conversation.objects.filter(
            organization=organization,
            channel=CHANNEL,
        ).values_list("external_id", flat=True)
    )
    known_messages = set(
        Message.objects.filter(
            organization=organization,
            conversation__channel=CHANNEL,
        ).values_list("external_id", flat=True)
    )
    for notification in notifications:
        pack_id = _pack_id(notification.resource)
        message_id = _message_id(notification.resource)
        if pack_id and pack_id in known_packs:
            continue
        if message_id and message_id in known_messages:
            continue
        if not pack_id and not message_id:
            continue
        try:
            loaded = _load_from_notification(
                organization,
                {"topic": "messages", "resource": notification.resource},
            )
        except (OSError, ValueError, InvalidToken):
            continue
        if loaded is not None:
            known_packs.add(loaded.external_id)


def sync_recent_postsale(organization):
    """Packs from orders in the last 7 days, plus any unread pack."""
    account = _account_for(organization)
    if account is None:
        raise ValueError("Mercado Libre is not connected.")
    token = account.fresh_access_token()
    seller_id = _ident(account.meli_user_id)
    until = timezone.now()
    since = until - WINDOW
    orders = fetch_recent_orders(token, seller_id, since, until)
    unread = fetch_unread_post_sale(token)
    seen = set()
    for order in orders:
        created = _when(order.get("date_created"))
        if created is not None and created < since:
            continue
        pack_id = _ident(order.get("pack_id")) or _ident(order.get("id"))
        if not pack_id or pack_id in seen:
            continue
        seen.add(pack_id)
        _load_quietly(organization, pack_id, order)
    for pack_id in _unread_pack_ids(unread):
        if pack_id in seen:
            continue
        seen.add(pack_id)
        _load_quietly(organization, pack_id, None)


def remember_reply(conversation, text, sent_at, message_id=""):
    messages = [
        {
            "external_id": message.external_id,
            "direction": message.direction,
            "body": message.body,
            "sent_at": message.sent_at,
        }
        for message in conversation.messages.exclude(external_id="pending")
    ]
    messages.append(
        {
            "external_id": message_id or f"local:{conversation.external_id}:{int(sent_at.timestamp())}",
            "direction": Message.Direction.OUTBOUND,
            "body": text,
            "sent_at": sent_at,
        }
    )
    return upsert_thread(
        organization=conversation.organization,
        channel=conversation.channel,
        external_id=conversation.external_id,
        title=conversation.title,
        preview=text,
        link_url=conversation.link_url,
        needs_reply=False,
        last_message_at=sent_at,
        messages=messages,
    )


def postsale_sync_action(organization):
    if organization is None:
        return None
    if not MercadoLibreAccount.objects.filter(organization=organization).exists():
        return None
    from django.urls import reverse

    return {
        "label": "Get recent post-sale messages",
        "url": reverse("mercadolibre_postsale_sync"),
    }


def buyer_id(thread, seller_id):
    seller_id = _ident(seller_id)
    chosen = ""
    chosen_at = _OLDEST
    for message in thread.get("messages") or []:
        if not isinstance(message, dict):
            continue
        sender = _party_id(message, "from")
        if not sender or sender == seller_id:
            continue
        sent_at = _message_when(message) or _OLDEST
        if chosen and sent_at < chosen_at:
            continue
        chosen = sender
        chosen_at = sent_at
    return chosen


def _load_quietly(organization, pack_id, order):
    from cryptography.fernet import InvalidToken

    try:
        load_pack(organization, pack_id, order)
    except (OSError, ValueError, InvalidToken):
        return None


def _account_for(organization):
    try:
        return organization.mercadolibre_account
    except MercadoLibreAccount.DoesNotExist:
        return None


def _bubbles(messages, seller_id):
    bubbles = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        body = _body(message)
        message_id = _ident(message.get("id"))
        if not body or not message_id:
            continue
        sender = _party_id(message, "from")
        direction = (
            Message.Direction.OUTBOUND
            if sender and sender == seller_id
            else Message.Direction.INBOUND
        )
        bubbles.append(
            {
                "external_id": message_id,
                "direction": direction,
                "body": body,
                "sent_at": _message_when(message),
            }
        )
    return bubbles


def _body(message):
    text = str(message.get("text") or "").strip()
    if text:
        return text
    attachments = message.get("message_attachments")
    if not isinstance(attachments, list):
        attachments = message.get("attachments")
    if isinstance(attachments, list) and attachments:
        return "Attachment"
    return ""


def _sale_title(order):
    if not isinstance(order, dict):
        return ""
    buyer = order.get("buyer") if isinstance(order.get("buyer"), dict) else {}
    name = " ".join(
        part
        for part in (_ident(buyer.get("first_name")), _ident(buyer.get("last_name")))
        if part
    ).strip()
    if not name:
        name = _ident(buyer.get("nickname") or buyer.get("id"))
    item = ""
    items = order.get("order_items")
    if isinstance(items, list) and items and isinstance(items[0], dict):
        info = items[0].get("item") if isinstance(items[0].get("item"), dict) else {}
        item = _ident(info.get("title"))
    if name and item:
        return f"{name} · {item}"
    return name or item


def _link(order):
    if not isinstance(order, dict):
        return ""
    permalink = _ident(order.get("permalink"))
    if permalink.startswith(("http://", "https://")):
        return permalink
    return ""


def sale_link(site_id, sale_id):
    domain = SITE_DOMAINS.get(_ident(site_id).upper())
    sale_id = _ident(sale_id)
    if not domain or not sale_id.isdigit():
        return ""
    return f"https://www.mercadolibre.{domain}/ventas/{sale_id}/detalle"


def _sale_link(organization, sale_id):
    site_id = (
        SellerProfile.objects.filter(organization=organization)
        .values_list("site_id", flat=True)
        .first()
    )
    return sale_link(site_id, sale_id)


def _is_active(status):
    if not isinstance(status, dict):
        return True
    value = _ident(status.get("status")).lower()
    if not value:
        return True
    return value == "active"


def _order_for_pack(token, pack_id):
    try:
        order = fetch_order(token, pack_id)
        if isinstance(order, dict) and order.get("id"):
            return order
    except (OSError, ValueError):
        order = None
    try:
        pack = fetch_pack(token, pack_id)
    except (OSError, ValueError):
        return {}
    orders = pack.get("orders") if isinstance(pack, dict) else None
    if not isinstance(orders, list) or not orders or not isinstance(orders[0], dict):
        return {}
    order_id = orders[0].get("id")
    if not order_id:
        return {}
    try:
        order = fetch_order(token, order_id)
    except (OSError, ValueError):
        return {}
    return order if isinstance(order, dict) else {}


def _unread_pack_ids(payload):
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        raise ValueError("Mercado Libre response had no unread messages.")
    packs = []
    for row in results:
        if not isinstance(row, dict):
            continue
        pack_id = _pack_id(row.get("resource"))
        if pack_id and pack_id not in packs:
            packs.append(pack_id)
    return packs


def _pack_from_message(message):
    """The pack id. A sale without a pack is messaged under its order id."""
    resources = message.get("message_resources") if isinstance(message, dict) else None
    if not isinstance(resources, list):
        return ""
    ids = {}
    for resource in resources:
        if isinstance(resource, dict):
            ids.setdefault(_ident(resource.get("name")).lower(), _ident(resource.get("id")))
    return ids.get("packs") or ids.get("pack") or ids.get("orders") or ids.get("order") or ""


def _pack_id(resource):
    match = _PACK_RESOURCE.search(str(resource or ""))
    return match.group(1) if match else ""


def _message_id(resource):
    resource = str(resource or "")
    match = _MESSAGE_RESOURCE.fullmatch(resource)
    if match:
        return match.group(1)
    return resource if _BARE_MESSAGE.fullmatch(resource) else ""


def _party_id(message, key):
    party = message.get(key) if isinstance(message, dict) else None
    if not isinstance(party, dict):
        return ""
    return _ident(party.get("user_id") if party.get("user_id") is not None else party.get("id"))


def _message_when(message):
    dates = message.get("message_date") if isinstance(message, dict) else None
    if not isinstance(dates, dict):
        dates = {}
    for key in ("created", "received", "available"):
        parsed = _when(dates.get(key))
        if parsed is not None:
            return parsed
    return None


def _when(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, dt_timezone.utc)
    return parsed


def _ident(value):
    if value is None or isinstance(value, bool):
        return ""
    return str(value).strip()
