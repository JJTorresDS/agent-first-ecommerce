from django.db import transaction
from django.utils import timezone

from .models import Conversation, Message


def _clip(value, limit):
    if value is None:
        return ""
    return str(value)[:limit]


@transaction.atomic
def upsert_thread(
    *,
    organization,
    channel,
    external_id,
    title,
    preview,
    link_url,
    needs_reply,
    last_message_at,
    messages,
    counterpart_id="",
):
    """Create or update one conversation and replace its bubbles.

    Messages omitted from the list are deleted. An empty answer therefore
    drops the outbound bubble on the next projection.
    """
    conversation, _created = Conversation.objects.update_or_create(
        organization=organization,
        channel=channel,
        external_id=_clip(external_id, 64),
        defaults={
            "title": _clip(title, 200),
            "preview": _clip(preview, 200),
            "link_url": _clip(link_url, 512),
            "needs_reply": needs_reply,
            "last_message_at": last_message_at,
            **({"counterpart_id": _clip(counterpart_id, 64)} if counterpart_id else {}),
        },
    )
    # One query reads the chat, then one bulk insert, one bulk update and one delete.
    # Each query is a round trip to Neon, so never one query per message.
    wanted = {}
    for item in messages:
        wanted[_clip(item["external_id"], 80)] = item
    existing = {message.external_id: message for message in conversation.messages.all()}
    now = timezone.now()
    created, changed = [], []
    for key, item in wanted.items():
        body = item.get("body") or ""
        direction = item["direction"]
        sent_at = item.get("sent_at")
        message = existing.get(key)
        if message is None:
            created.append(
                Message(
                    organization=organization,
                    conversation=conversation,
                    external_id=key,
                    direction=direction,
                    body=body,
                    sent_at=sent_at,
                    created_at=now,
                    updated_at=now,
                )
            )
        elif (message.body, message.direction, message.sent_at) != (body, direction, sent_at):
            message.body, message.direction, message.sent_at = body, direction, sent_at
            # bulk_update skips auto_now, and the inbox poll's version reads updated_at.
            message.updated_at = now
            changed.append(message)
    if created:
        Message.objects.bulk_create(created)
    if changed:
        Message.objects.bulk_update(changed, ["body", "direction", "sent_at", "updated_at"])
    stale = [message.pk for key, message in existing.items() if key not in wanted]
    if stale:
        Message.objects.filter(pk__in=stale).delete()
    return conversation
