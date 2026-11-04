import hashlib
from datetime import timedelta
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.models import membership_for

from .channels import can_send, list_actions, reply_limit_for
from .models import Conversation, Message


def inbox_location(*, conversation=None, failed=False, sync=""):
    params = []
    if conversation is not None:
        params.append(("conversation", str(conversation.pk)))
    if failed:
        params.append(("reply", "failed"))
    if sync:
        params.append(("sync", sync))
    url = reverse("inbox")
    if not params:
        return url
    return f"{url}?{urlencode(params)}"


def _selected(conversations, selected_id):
    if not selected_id:
        return None
    try:
        return conversations.filter(pk=selected_id).first()
    except (ValidationError, ValueError):
        return None


def _save_pending(conversation, text):
    Message.objects.update_or_create(
        conversation=conversation,
        external_id="pending",
        defaults={
            "organization": conversation.organization,
            "direction": Message.Direction.OUTBOUND,
            "body": text,
            "sent_at": timezone.now(),
        },
    )
    conversation.preview = text[:200]
    conversation.save(update_fields=["preview", "updated_at"])


def send_and_redirect(request, conversation):
    from meli.jobs import clear_reply_error, dispatch_reply, reply_status

    text = (request.POST.get("text") or "").strip()
    failed = False
    if can_send(conversation):
        if not text or len(text) > reply_limit_for(conversation.channel):
            failed = True
        elif reply_status(conversation) not in {"queued", "running"}:
            clear_reply_error(conversation)
            _save_pending(conversation, text)
            failed = dispatch_reply(conversation) == "failed"
            conversation.refresh_from_db()
    if request.headers.get("HX-Request") == "true":
        return _render_thread(request, conversation, failed=failed)
    return redirect(
        inbox_location(
            conversation=conversation,
            failed=failed,
        )
    )


def _thread_request(request):
    return (
        request.headers.get("HX-Request") == "true"
        and request.headers.get("HX-Target") == "thread"
    )


def _list_poll(request):
    return (
        request.headers.get("HX-Request") == "true"
        and request.headers.get("HX-Target") == "chat-list"
    )


def _active_first(conversations, selected_id):
    """The rows in their usual order, with the selected one moved to the top."""
    rows = list(conversations)
    selected = next((row for row in rows if str(row.pk) == selected_id), None)
    if selected is None:
        return rows, rows[0] if rows else None
    return [selected] + [row for row in rows if row.pk != selected.pk], selected


def _list_version(rows, context):
    """A fingerprint of what the list poll shows, so an unchanged poll can answer 204."""
    selected = context["selected"]
    parts = [str(selected.pk) if selected else ""]
    parts += [f"{row.pk}:{row.updated_at.isoformat()}:{row.needs_reply}:{row.preview}" for row in rows]
    parts += [f"{message.pk}:{message.updated_at.isoformat()}" for message in context["messages"]]
    parts += [
        str(context[key])
        for key in ("can_reply", "reply_pending", "reply_error", "reply_error_message")
    ]
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()[:16]


def _thread_context(organization, conversation, *, reply_error=False):
    from meli.jobs import reply_error as stored_reply_error
    from meli.jobs import reply_status

    messages = []
    can_reply = False
    reply_limit = 2000
    status = ""
    refused = None
    if conversation is not None:
        messages = conversation.messages.filter(organization=organization)
        status = reply_status(conversation)
        can_reply = can_send(conversation)
        reply_limit = reply_limit_for(conversation.channel)
        if status not in {"queued", "running"}:
            refused = stored_reply_error(conversation)
    return {
        "selected": conversation,
        "messages": messages,
        "can_reply": can_reply,
        "reply_limit": reply_limit,
        "reply_error": reply_error or status == "failed" or refused is not None,
        "reply_error_message": (refused or {}).get("message", ""),
        "reply_error_text": (refused or {}).get("text", ""),
        "reply_pending": status in {"queued", "running"},
    }


def _render_thread(request, conversation, *, failed=False):
    organization = conversation.organization
    context = _thread_context(organization, conversation, reply_error=failed)
    context["oob"] = True
    request.inbox_partial = True
    return render(request, "inbox/_thread.html", context)


@login_required
def inbox(request):
    membership = membership_for(request.user)
    if membership is None:
        return redirect("organization_setup")
    organization = membership.organization
    from meli.jobs import dispatch_refresh

    thread_request = _thread_request(request)
    list_poll = _list_poll(request)
    if thread_request or list_poll:
        # Partials skip the nav context processor and its queries.
        request.inbox_partial = True
    if not thread_request:
        dispatch_refresh(organization)
    show_older = request.GET.get("since") == "all"
    conversations = Conversation.objects.filter(organization=organization)
    if not show_older:
        cutoff = timezone.now() - timedelta(days=7)
        conversations = conversations.filter(
            Q(needs_reply=True)
            | Q(last_message_at__gte=cutoff)
            | Q(last_message_at__isnull=True, created_at__gte=cutoff)
        )
    conversations = conversations.order_by(
        "-needs_reply",
        "-last_message_at",
        "-created_at",
    )
    selected_id = request.GET.get("conversation") or ""
    if thread_request:
        selected = _selected(conversations, selected_id) or conversations.first()
        context = {
            "oob": True,
            # The "Sending…" poll keeps a draft typed meanwhile. A row click gets a fresh composer.
            "preserve_composer": request.GET.get("poll") == "sending",
            **_thread_context(
                organization,
                selected,
                reply_error=request.GET.get("reply") == "failed",
            ),
        }
        return render(request, "inbox/_thread.html", context)
    rows, selected = _active_first(conversations, selected_id)
    context = {
        "conversations": rows,
        "show_older": show_older,
        "list_poll": list_poll,
        **_thread_context(
            organization,
            selected,
            reply_error=request.GET.get("reply") == "failed",
        ),
    }
    context["messages"] = list(context["messages"])
    context["list_version"] = _list_version(rows, context)
    if list_poll:
        if request.headers.get("X-Inbox-Version") == context["list_version"]:
            return HttpResponse(status=204)
        return render(request, "inbox/_list.html", context)
    context["list_actions"] = list_actions(organization)
    context.update(_sync_flags(organization, request.GET.get("sync") or ""))
    return render(request, "inbox/inbox.html", context)


def _sync_flags(organization, query_sync):
    from meli.jobs import postsale_status

    postsale = postsale_status(organization)
    pending = postsale in {"queued", "running"}
    return {
        "sync_pending": pending,
        "postsale_error": postsale == "failed",
        "postsale_ok": postsale == "ok",
        "sync_error": query_sync == "failed" and postsale != "failed" and not pending,
        "sync_ok": query_sync == "ok" and postsale != "ok" and not pending,
    }


@login_required
@require_POST
def inbox_reply(request, conversation_id):
    membership = membership_for(request.user)
    if membership is None:
        return redirect("organization_setup")
    conversation = get_object_or_404(
        Conversation,
        organization=membership.organization,
        pk=conversation_id,
    )
    return send_and_redirect(request, conversation)
