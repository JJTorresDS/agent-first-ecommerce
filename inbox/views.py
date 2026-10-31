from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from accounts.models import membership_for

from .channels import ReplyFailed, list_actions, open_inbox, sender_for
from .models import Conversation


def inbox_location(*, channel="", conversation=None, failed=False, sync=""):
    params = []
    chosen = channel or (conversation.channel if conversation is not None else "")
    if chosen:
        params.append(("channel", chosen))
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


def send_and_redirect(request, conversation):
    text = request.POST.get("text") or ""
    send = sender_for(conversation.channel)
    failed = False
    if conversation.needs_reply and send is not None:
        try:
            send(conversation, text)
        except ReplyFailed:
            failed = True
    return redirect(
        inbox_location(
            channel=conversation.channel,
            conversation=conversation,
            failed=failed,
        )
    )


@login_required
def inbox(request):
    membership = membership_for(request.user)
    if membership is None:
        return redirect("organization_setup")
    organization = membership.organization
    open_inbox(organization)
    channel = request.GET.get("channel") or ""
    conversations = Conversation.objects.filter(organization=organization)
    channels = list(
        conversations.order_by("channel").values_list("channel", flat=True).distinct()
    )
    if channel and channel not in channels:
        channels.append(channel)
    if channel:
        conversations = conversations.filter(channel=channel)
    selected = _selected(conversations, request.GET.get("conversation") or "")
    if selected is None:
        selected = conversations.first()
    messages = []
    can_reply = False
    if selected is not None:
        messages = selected.messages.filter(organization=organization)
        can_reply = selected.needs_reply and sender_for(selected.channel) is not None
    return render(
        request,
        "inbox/inbox.html",
        {
            "conversations": conversations,
            "channels": channels,
            "channel": channel,
            "selected": selected,
            "messages": messages,
            "can_reply": can_reply,
            "list_actions": list_actions(organization),
            "reply_error": request.GET.get("reply") == "failed",
            "sync_error": request.GET.get("sync") == "failed",
            "sync_ok": request.GET.get("sync") == "ok",
        },
    )


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
