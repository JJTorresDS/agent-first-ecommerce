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


def postsale_messages(organization):
    count = postsale_count(organization)
    if not count:
        return None
    noun = "message" if count == 1 else "messages"
    return {
        "count": count,
        "message": f"You have {count} unanswered post-sale {noun}",
        "url": reverse("inbox"),
    }


def presales_questions(organization):
    count = unanswered_count(organization)
    if not count:
        return None
    noun = "question" if count == 1 else "questions"
    return {
        "count": count,
        "message": f"You have {count} unanswered presales {noun}",
        "url": reverse("inbox"),
    }
