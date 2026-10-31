from django.db import transaction

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
        },
    )
    keep = []
    for item in messages:
        message, _created = Message.objects.update_or_create(
            conversation=conversation,
            external_id=_clip(item["external_id"], 80),
            defaults={
                "organization": organization,
                "direction": item["direction"],
                "body": item.get("body") or "",
                "sent_at": item.get("sent_at"),
            },
        )
        keep.append(message.pk)
    stale = conversation.messages.all()
    if keep:
        stale = stale.exclude(pk__in=keep)
    stale.delete()
    return conversation
