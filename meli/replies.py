from django.utils import timezone

from cryptography.fernet import InvalidToken

from inbox.channels import ReplyFailed

from .client import post_answer
from .models import ItemQuestion


def send_item_question(conversation, text):
    from .projection import project_question
    from .views import _account_for, _load_question

    question = ItemQuestion.objects.filter(
        organization_id=conversation.organization_id,
        question_id=conversation.external_id,
    ).first()
    if question is None or not question.can_reply():
        return
    cleaned = (text or "").strip()
    if not cleaned or len(cleaned) > 2000:
        raise ReplyFailed()
    account = _account_for(question.organization)
    if account is None:
        raise ReplyFailed()
    try:
        post_answer(account.fresh_access_token(), question.question_id, cleaned)
    except (OSError, ValueError, InvalidToken):
        raise ReplyFailed() from None
    question.answer_text = cleaned
    question.status = "ANSWERED"
    question.answered_at = timezone.now()
    question.save(update_fields=["answer_text", "status", "answered_at", "updated_at"])
    try:
        _load_question(question.organization, question.question_id)
    except (OSError, ValueError, InvalidToken):
        project_question(question)
