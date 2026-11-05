from django.db.models import Count

from .models import Conversation


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
