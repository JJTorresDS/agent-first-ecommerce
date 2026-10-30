from django.urls import reverse

from .models import ItemQuestion


def unanswered_count(organization):
    if organization is None:
        return 0
    return ItemQuestion.objects.filter(
        organization=organization,
        status__iexact="UNANSWERED",
        answer_text="",
    ).count()


def presales_questions(organization):
    count = unanswered_count(organization)
    if not count:
        return None
    noun = "question" if count == 1 else "questions"
    return {
        "count": count,
        "message": f"You have {count} unanswered presales {noun}",
        "url": reverse("mercadolibre_questions"),
    }
