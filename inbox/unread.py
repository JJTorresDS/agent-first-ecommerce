from django.db.models import Count, Max

from .models import Conversation, Message


def waiting_counts(user):
    """{organization_id: {channel: chats waiting for a reply}}, over the user's own organizations only. One query.

    The nav reads both the badges and the alerts from it.
    """
    if not getattr(user, "is_authenticated", False):
        return {}
    rows = (
        Conversation.objects.filter(organization__memberships__user=user, needs_reply=True)
        .values("organization_id", "channel")
        .annotate(count=Count("pk", distinct=True))
    )
    counts = {}
    for row in rows:
        counts.setdefault(row["organization_id"], {})[row["channel"]] = row["count"]
    return counts


def unread_by_organization(user, waiting=None):
    """{organization_id: chats waiting for a reply}, over the user's own organizations only. One query."""
    if waiting is None:
        waiting = waiting_counts(user)
    return {organization_id: sum(channels.values()) for organization_id, channels in waiting.items()}


def latest_incoming(user):
    """When the newest message from a buyer arrived, over the user's own organizations. One query.

    The chime follows this, so a second message in a chat that already waits still sounds.
    """
    if not getattr(user, "is_authenticated", False):
        return None
    return Message.objects.filter(
        organization__memberships__user=user,
        direction=Message.Direction.INBOUND,
    ).aggregate(latest=Max("created_at"))["latest"]
