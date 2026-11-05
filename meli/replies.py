from django.utils import timezone

from cryptography.fernet import InvalidToken

from inbox.channels import ReplyFailed

from .client import MercadoLibreError, post_answer
from .models import ItemQuestion

UNREACHABLE = "Mercado Libre could not be reached. Try again."
EXPIRED = "The Mercado Libre connection expired. Connect it again."
UNREADABLE = "The saved Mercado Libre connection can't be read on this server."


def _refused(error):
    """The reason to show for a failed call. Mercado Libre's own words when it gave any."""
    if isinstance(error, MercadoLibreError):
        return ReplyFailed(error.message)
    if isinstance(error, InvalidToken):
        return ReplyFailed(UNREADABLE)
    return ReplyFailed(UNREACHABLE)


def send_post_sale(conversation, text):
    """Post one message to the sale's chat.

    One Mercado Libre call when the buyer is already known. The reply is stored from
    Mercado Libre's answer at once; the full chat is re-read in the background.
    """
    from .client import fetch_pack_messages, post_pack_message
    from .jobs import dispatch_notification
    from .postsale import _message_when, buyer_id, remember_reply
    from .views import _account_for

    cleaned = (text or "").strip()
    if not cleaned or len(cleaned) > 350:
        raise ReplyFailed()
    account = _account_for(conversation.organization)
    if account is None:
        raise ReplyFailed(EXPIRED)
    seller_id = str(account.meli_user_id)
    try:
        token = account.fresh_access_token()
        recipient = conversation.counterpart_id
        if not recipient:
            thread = fetch_pack_messages(token, conversation.external_id, seller_id)
            recipient = buyer_id(thread, seller_id)
        if not recipient:
            raise ReplyFailed("The buyer of this sale could not be found.")
        sent = post_pack_message(
            token,
            conversation.external_id,
            seller_id,
            recipient,
            cleaned,
        )
    except ReplyFailed:
        raise
    except (OSError, ValueError, InvalidToken) as error:
        raise _refused(error) from None
    message = sent.get("message") if isinstance(sent.get("message"), dict) else sent
    message_id = str(message.get("id") or "")[:80]
    remember_reply(conversation, cleaned, _message_when(message) or timezone.now(), message_id)
    # The whole chat, for every organization with this seller, without making the sender wait.
    dispatch_notification(
        {"topic": "messages", "resource": f"/packs/{conversation.external_id}", "user_id": seller_id}
    )


def send_item_question(conversation, text):
    """Answer a presale question with one Mercado Libre call; the question is re-read in the background."""
    from .jobs import dispatch_notification
    from .projection import project_question
    from .views import _account_for

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
        raise ReplyFailed(EXPIRED)
    try:
        post_answer(account.fresh_access_token(), question.question_id, cleaned)
    except (OSError, ValueError, InvalidToken) as error:
        raise _refused(error) from None
    question.answer_text = cleaned
    question.status = "ANSWERED"
    question.answered_at = timezone.now()
    question.save(update_fields=["answer_text", "status", "answered_at", "updated_at"])
    project_question(question)
    dispatch_notification(
        {"topic": "questions", "resource": f"/questions/{question.question_id}", "user_id": account.meli_user_id}
    )
