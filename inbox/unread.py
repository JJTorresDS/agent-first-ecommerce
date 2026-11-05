from django.db.models import Count, Max, Q

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


def latest_incoming(user, since=None):
    """(When the newest message from a buyer arrived, how many arrived after since), over the user's own organizations. One query.

    The chime follows this, so a second message in a chat that already waits still sounds,
    and two questions in one poll sound twice.
    """
    if not getattr(user, "is_authenticated", False):
        return None, 0
    counts = {"latest": Max("created_at")}
    if since is not None:
        counts["new"] = Count("pk", distinct=True, filter=Q(created_at__gte=since))
    row = Message.objects.filter(
        organization__memberships__user=user,
        direction=Message.Direction.INBOUND,
    ).aggregate(**counts)
    return row["latest"], row.get("new") or 0
