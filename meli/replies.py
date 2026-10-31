from django.utils import timezone

from cryptography.fernet import InvalidToken

from inbox.channels import ReplyFailed

from .client import post_answer
from .models import ItemQuestion


def send_post_sale(conversation, text):
    from .client import fetch_pack_messages, post_pack_message
    from .postsale import buyer_id, load_pack, remember_reply
    from .views import _account_for

    cleaned = (text or "").strip()
    if not cleaned or len(cleaned) > 350:
        raise ReplyFailed()
    if not conversation.needs_reply:
        return
    account = _account_for(conversation.organization)
    if account is None:
        raise ReplyFailed()
    seller_id = str(account.meli_user_id)
    try:
        token = account.fresh_access_token()
        thread = fetch_pack_messages(token, conversation.external_id, seller_id)
        recipient = buyer_id(thread, seller_id)
        if not recipient:
            raise ReplyFailed()
        post_pack_message(
            token,
            conversation.external_id,
            seller_id,
            recipient,
            cleaned,
        )
    except ReplyFailed:
        raise
    except (OSError, ValueError, InvalidToken):
        raise ReplyFailed() from None
    try:
        load_pack(conversation.organization, conversation.external_id)
    except (OSError, ValueError, InvalidToken):
        remember_reply(conversation, cleaned, timezone.now())


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
