from django.urls import reverse

from inbox.models import Channel, Conversation


def unanswered_count(organization):
    if organization is None:
        return 0
    return Conversation.objects.filter(
        organization=organization,
        channel=Channel.MELI_ITEM_QUESTIONS,
        needs_reply=True,
    ).count()


def postsale_count(organization):
    if organization is None:
        return 0
    return Conversation.objects.filter(
        organization=organization,
        channel=Channel.MELI_POSTSALES,
        needs_reply=True,
    ).count()


def postsale_messages(organization, waiting=None):
    if waiting is None:
        count = postsale_count(organization)
    else:
        count = waiting.get(Channel.MELI_POSTSALES, 0)
    if not count:
        return None
    noun = "message" if count == 1 else "messages"
    return {
        "count": count,
        "message": f"You have {count} unanswered post-sale {noun}",
        "url": reverse("inbox"),
    }


def presales_questions(organization, waiting=None):
    if waiting is None:
        count = unanswered_count(organization)
    else:
        count = waiting.get(Channel.MELI_ITEM_QUESTIONS, 0)
    if not count:
        return None
    noun = "question" if count == 1 else "questions"
    return {
        "count": count,
        "message": f"You have {count} unanswered presales {noun}",
        "url": reverse("inbox"),
    }


def claims_due(organization, waiting=None):
    """Open claims with a deadline: the bell, the sidebar's Claims and the Claims tab show the count."""
    from .claim_views import deadline_text, time_left
    from .claims import due_summary

    count, first = due_summary(organization)
    if not count:
        return None
    noun = "claim waits" if count == 1 else "claims wait"
    left = time_left(first)
    when = "one is overdue" if left == "Overdue" else f"the first by {deadline_text(first)} ({left})"
    return {
        "kind": "claims",
        "count": count,
        "message": f"{count} Mercado Libre {noun} for your answer: {when}",
        "url": reverse("mercadolibre_claims"),
    }
