from inbox.models import Channel, Message
from inbox.services import upsert_thread

from .models import ItemQuestion, MercadoLibreAccount


def project_question(question):
    messages = [
        {
            "external_id": f"{question.question_id}:in",
            "direction": Message.Direction.INBOUND,
            "body": question.text,
            "sent_at": question.asked_at,
        }
    ]
    if question.answer_text:
        messages.append(
            {
                "external_id": f"{question.question_id}:out",
                "direction": Message.Direction.OUTBOUND,
                "body": question.answer_text,
                "sent_at": question.answered_at,
            }
        )
    upsert_thread(
        organization=question.organization,
        channel=Channel.MELI_ITEM_QUESTIONS,
        external_id=question.question_id,
        title=question.item_title or question.item_id or question.question_id,
        preview=question.answer_text or question.text,
        link_url=question.item_url(),
        needs_reply=question.can_reply(),
        last_message_at=question.answered_at or question.asked_at,
        messages=messages,
    )


def refresh_item_questions(organization):
    from .views import _backfill_questions

    _backfill_questions(organization)


def reconcile_item_questions(organization):
    if organization is None:
        return
    for question in ItemQuestion.objects.filter(organization=organization):
        project_question(question)


def questions_sync_action(organization):
    if organization is None:
        return None
    if not MercadoLibreAccount.objects.filter(organization=organization).exists():
        return None
    from django.urls import reverse

    return {
        "label": "Get open questions",
        "url": reverse("mercadolibre_questions_sync"),
    }
