from django.db.models import Count, Max

from .models import Conversation, Message


def unread_by_organization(user):
    """{organization_id: chats waiting for a reply}, over the user's own organizations only. One query."""
    if not getattr(user, "is_authenticated", False):
        return {}
    rows = (
        Conversation.objects.filter(organization__memberships__user=user, needs_reply=True)
        .values("organization_id")
        .annotate(count=Count("pk", distinct=True))
    )
    return {row["organization_id"]: row["count"] for row in rows}


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
