import hmac
import json
import logging
import re
from datetime import datetime, timezone as dt_timezone
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.db import IntegrityError, transaction
from django.http import HttpResponse, HttpResponseForbidden, HttpResponseNotFound
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from accounts.models import can_connect_mercadolibre, membership_for

from cryptography.fernet import InvalidToken

from .alerts import unanswered_count
from .client import (
    exchange_code,
    fetch_item,
    fetch_question,
    fetch_unanswered_questions,
    fetch_user_profile,
)
from .models import (
    ItemQuestion,
    MercadoLibreAccount,
    MercadoLibreNotification,
    SellerProfile,
)

logger = logging.getLogger(__name__)


def _clip(value, limit):
    if value is None:
        return ""
    return str(value)[:limit]


def _attempts(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _when(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, dt_timezone.utc)
    return parsed


def _organization_for(user_id):
    if not user_id:
        return None
    account = (
        MercadoLibreAccount.objects.filter(meli_user_id=user_id)
        .select_related("organization")
        .first()
    )
    return account.organization if account else None


def _webhook_secret_matches(secret):
    expected = settings.MELI_WEBHOOK_SECRET
    if not expected or not secret:
        return False
    return hmac.compare_digest(secret, expected)


def _store_object(parsed):
    application_id = str(parsed.get("application_id") or "")
    if not settings.MELI_CLIENT_ID or application_id != settings.MELI_CLIENT_ID:
        return
    notification_id = _clip(parsed.get("_id"), 128)
    user_id = _clip(parsed.get("user_id"), 64)
    organization = _organization_for(user_id)
    if organization is None:
        return
    fields = {
        "topic": _clip(parsed.get("topic"), 64),
        "resource": _clip(parsed.get("resource"), 512),
        "user_id": user_id,
        "application_id": _clip(application_id, 64),
        "attempts": _attempts(parsed.get("attempts")),
        "sent_at": _when(parsed.get("sent")),
        "received_at": _when(parsed.get("received")),
        "payload": parsed,
        "organization": organization,
    }
    if not notification_id:
        MercadoLibreNotification.objects.create(notification_id="", **fields)
        return
    try:
        with transaction.atomic():
            MercadoLibreNotification.objects.get_or_create(
                notification_id=notification_id,
                defaults=fields,
            )
    except IntegrityError:
        return


@csrf_exempt
@require_POST
def mercadolibre_webhook(request, secret):
    if not _webhook_secret_matches(secret):
        return HttpResponseNotFound()
    raw = request.body.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        _store_object(parsed)
        from .jobs import dispatch_notification

        dispatch_notification(parsed)
    return HttpResponse("ok")


def _owner_organization(request):
    membership = membership_for(request.user)
    if membership is None:
        return None, redirect("organization_setup")
    if not can_connect_mercadolibre(membership):
        return None, HttpResponseForbidden(
            "Only an owner or admin can connect Mercado Libre."
        )
    return membership.organization, None


def _callback_uri(request):
    return request.build_absolute_uri(reverse("mercadolibre_callback"))


def _settings_redirect(code=""):
    url = reverse("mercadolibre_settings")
    if code:
        return redirect(f"{url}?meli={code}")
    return redirect(url)


def _account_for(organization):
    try:
        return organization.mercadolibre_account
    except MercadoLibreAccount.DoesNotExist:
        return None


_MELI_ERRORS = {
    "denied": "Mercado Libre did not authorize the connection.",
    "taken": "That Mercado Libre account is already connected to another organization.",
    "failed": "Mercado Libre could not be connected.",
    "profile": "The seller profile could not be loaded.",
}


def _question_id(resource):
    match = re.fullmatch(r"/questions/(\d+)", str(resource or ""))
    return match.group(1) if match else ""


def _upsert_question(organization, payload, item):
    question_id = str(payload.get("id") or "")
    if not question_id:
        return None
    answer = payload.get("answer") if isinstance(payload.get("answer"), dict) else {}
    buyer = payload.get("from") if isinstance(payload.get("from"), dict) else {}
    permalink = str(item.get("permalink") or "")
    if not permalink.startswith(("http://", "https://")):
        permalink = ""
    defaults = {
        "organization": organization,
        "item_id": _clip(payload.get("item_id"), 32),
        "item_title": _clip(item.get("title"), 200),
        "item_permalink": _clip(permalink, 512),
        "text": str(payload.get("text") or ""),
        "status": _clip(payload.get("status"), 32),
        "from_user_id": _clip(buyer.get("id"), 64),
        "answer_text": str(answer.get("text") or ""),
        "asked_at": _when(payload.get("date_created")),
        "answered_at": _when(answer.get("date_created")) if answer else None,
    }
    question, _created = ItemQuestion.objects.update_or_create(
        question_id=question_id,
        defaults=defaults,
    )
    from .projection import project_question

    project_question(question)
    return question


def _load_question(organization, question_id):
    account = _account_for(organization)
    if account is None:
        return None
    token = account.fresh_access_token()
    payload = fetch_question(token, question_id)
    item = {}
    item_id = str(payload.get("item_id") or "")
    if item_id:
        try:
            item = fetch_item(token, item_id)
        except (OSError, ValueError):
            item = {}
    return _upsert_question(organization, payload, item)


def _load_question_from_notification(parsed):
    if _clip(parsed.get("topic"), 64) != "questions":
        return
    question_id = _question_id(parsed.get("resource"))
    if not question_id:
        return
    organization = _organization_for(_clip(parsed.get("user_id"), 64))
    if organization is None:
        return
    _load_question(organization, question_id)


def _backfill_questions(organization):
    if _account_for(organization) is None:
        return
    known = set(
        ItemQuestion.objects.filter(organization=organization).values_list(
            "question_id",
            flat=True,
        )
    )
    notifications = MercadoLibreNotification.objects.filter(
        organization=organization,
        topic="questions",
    )
    for notification in notifications:
        question_id = _question_id(notification.resource)
        if not question_id or question_id in known:
            continue
        try:
            _load_question(organization, question_id)
        except (OSError, ValueError, InvalidToken):
            continue
        known.add(question_id)


def _seller_snippet(access_token):
    return (
        "curl https://api.mercadolibre.com/users/me \\\n"
        f'  -H "Authorization: Bearer {access_token}"'
    )


def _active_profile(organization):
    try:
        profile = organization.seller_profile
    except SellerProfile.DoesNotExist:
        return None
    if profile.deleted_at:
        return None
    return profile


def _save_seller_profile(organization, payload):
    profile = SellerProfile.objects.filter(organization=organization).first()
    if profile is None:
        profile = SellerProfile(organization=organization)
    profile.store_user_profile(payload)
    profile.save()
    return profile


def _load_seller_profile(account):
    _save_seller_profile(
        account.organization,
        fetch_user_profile(account.fresh_access_token()),
    )


def _settings_context(membership, meli_error="", access_token=""):
    return {
        "can_connect": can_connect_mercadolibre(membership),
        "meli_account": _account_for(membership.organization),
        "seller_profile": _active_profile(membership.organization),
        "meli_error": meli_error,
        "access_token": access_token,
        "snippet": _seller_snippet(access_token) if access_token else "",
        "active_tab": "settings",
        "open_question_count": unanswered_count(membership.organization),
    }


@login_required
def mercadolibre_settings(request):
    membership = membership_for(request.user)
    if membership is None:
        return redirect("organization_setup")
    error = _MELI_ERRORS.get(request.GET.get("meli"), "")
    account = _account_for(membership.organization)
    if account is not None and _active_profile(membership.organization) is None:
        try:
            _load_seller_profile(account)
        except (OSError, ValueError, InvalidToken):
            error = error or _MELI_ERRORS["profile"]
    return render(request, "meli/settings.html", _settings_context(membership, error))


@login_required
def mercadolibre_connect(request):
    organization, denied = _owner_organization(request)
    if denied:
        return denied
    if not settings.MELI_CLIENT_ID:
        return _settings_redirect("failed")
    state = signing.dumps({"organization": str(organization.id)})
    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.MELI_CLIENT_ID,
            "redirect_uri": _callback_uri(request),
            "state": state,
        }
    )
    return redirect(f"{settings.MELI_AUTH_URL}?{query}")


@login_required
def mercadolibre_callback(request):
    organization, denied = _owner_organization(request)
    if denied:
        return denied
    if request.GET.get("error") or not request.GET.get("code"):
        return _settings_redirect("denied")
    try:
        payload = signing.loads(request.GET.get("state", ""), max_age=600)
    except signing.BadSignature:
        return HttpResponseForbidden("The Mercado Libre connection expired.")
    if payload.get("organization") != str(organization.id):
        return HttpResponseForbidden("The Mercado Libre connection expired.")
    try:
        token = exchange_code(request.GET["code"], _callback_uri(request))
    except (OSError, ValueError):
        return _settings_redirect("failed")
    user_id = str(token.get("user_id") or "")
    if not user_id:
        return _settings_redirect("failed")
    taken = MercadoLibreAccount.objects.filter(meli_user_id=user_id).exclude(
        organization=organization
    )
    if taken.exists():
        return _settings_redirect("taken")
    account = MercadoLibreAccount.objects.filter(organization=organization).first()
    if account is None:
        account = MercadoLibreAccount(organization=organization, meli_user_id=user_id)
    account.meli_user_id = user_id
    account.store_token_response(token)
    try:
        account.save()
    except IntegrityError:
        return _settings_redirect("taken")
    MercadoLibreNotification.objects.filter(
        user_id=user_id,
        organization__isnull=True,
    ).update(organization=organization)
    try:
        _load_seller_profile(account)
    except (OSError, ValueError, InvalidToken):
        return _settings_redirect("profile")
    return _settings_redirect()


@login_required
@require_POST
def mercadolibre_disconnect(request):
    organization, denied = _owner_organization(request)
    if denied:
        return denied
    SellerProfile.objects.filter(
        organization=organization,
        deleted_at__isnull=True,
    ).update(deleted_at=timezone.now())
    MercadoLibreAccount.objects.filter(organization=organization).delete()
    return _settings_redirect()


@login_required
@require_POST
def mercadolibre_token(request):
    organization, denied = _owner_organization(request)
    if denied:
        return denied
    membership = membership_for(request.user)
    account = _account_for(organization)
    if account is None:
        return _settings_redirect()
    try:
        token = account.fresh_access_token()
    except (OSError, ValueError, InvalidToken):
        return render(
            request,
            "meli/settings.html",
            _settings_context(
                membership,
                "The access token could not be refreshed.",
            ),
        )
    return render(
        request,
        "meli/settings.html",
        _settings_context(membership, access_token=token),
    )


def _item_for_sync(token, item_id, existing, cache):
    if existing is not None and existing.item_id == item_id and existing.item_title:
        return {"title": existing.item_title, "permalink": existing.item_permalink}
    if item_id in cache:
        return cache[item_id]
    try:
        item = fetch_item(token, item_id)
    except (OSError, ValueError):
        item = {}
    cache[item_id] = item
    return item


def _sync_open_questions(organization):
    account = _account_for(organization)
    if account is None:
        raise ValueError("Mercado Libre is not connected.")
    token = account.fresh_access_token()
    payload = fetch_unanswered_questions(token, account.meli_user_id)
    questions = payload.get("questions")
    if not isinstance(questions, list):
        raise ValueError("Mercado Libre response had no questions.")
    seller_id = str(account.meli_user_id)
    items = {}
    for raw in questions:
        if not isinstance(raw, dict):
            continue
        question_id = str(raw.get("id") or "")
        if not question_id:
            continue
        seller = str(raw.get("seller_id") or "")
        if seller and seller != seller_id:
            continue
        existing = ItemQuestion.objects.filter(question_id=question_id).first()
        if existing is not None and existing.organization_id != organization.id:
            continue
        item_id = str(raw.get("item_id") or "")
        item = _item_for_sync(token, item_id, existing, items) if item_id else {}
        _upsert_question(organization, raw, item)


def _inbox_for_question(organization, question_id, *, failed=False, sync=""):
    from inbox.models import Channel, Conversation
    from inbox.views import inbox_location

    channel = Channel.MELI_ITEM_QUESTIONS
    conversation = None
    if question_id:
        conversation = Conversation.objects.filter(
            organization=organization,
            channel=channel,
            external_id=question_id,
        ).first()
    return redirect(
        inbox_location(
            conversation=conversation,
            failed=failed,
            sync=sync,
        )
    )


@login_required
def mercadolibre_questions(request):
    membership = membership_for(request.user)
    if membership is None:
        return redirect("organization_setup")
    organization = membership.organization
    _backfill_questions(organization)
    from inbox.channels import reconcile

    reconcile(organization)
    sync = request.GET.get("sync") or ""
    if sync not in {"ok", "failed"}:
        sync = ""
    return _inbox_for_question(
        organization,
        request.GET.get("question") or "",
        failed=request.GET.get("reply") == "failed",
        sync=sync,
    )


@login_required
@require_POST
def mercadolibre_questions_sync(request):
    membership = membership_for(request.user)
    if membership is None:
        return redirect("organization_setup")
    failed = False
    try:
        _sync_open_questions(membership.organization)
    except (OSError, ValueError, InvalidToken):
        failed = True
    return _inbox_for_question(
        membership.organization,
        "",
        sync="failed" if failed else "ok",
    )


@login_required
@require_POST
def mercadolibre_question_reply(request, question_id):
    membership = membership_for(request.user)
    if membership is None:
        return redirect("organization_setup")
    question = get_object_or_404(
        ItemQuestion,
        organization=membership.organization,
        question_id=question_id,
    )
    from inbox.models import Channel, Conversation
    from inbox.views import send_and_redirect

    from .projection import project_question

    project_question(question)
    conversation = Conversation.objects.get(
        organization=membership.organization,
        channel=Channel.MELI_ITEM_QUESTIONS,
        external_id=question.question_id,
    )
    return send_and_redirect(request, conversation)


@login_required
@require_POST
def mercadolibre_postsale_sync(request):
    membership = membership_for(request.user)
    if membership is None:
        return redirect("organization_setup")
    from inbox.views import inbox_location

    from .jobs import enqueue_postsale_sync, postsale_queue_enabled
    from .postsale import sync_recent_postsale

    if postsale_queue_enabled():
        failed = False
        try:
            enqueue_postsale_sync(membership.organization)
        except Exception:
            logger.exception("Could not queue the post-sale sync")
            failed = True
        return redirect(
            inbox_location(
                sync="failed" if failed else "queued",
            )
        )

    failed = False
    try:
        sync_recent_postsale(membership.organization)
    except (OSError, ValueError, InvalidToken):
        failed = True
    return redirect(
        inbox_location(
            sync="failed" if failed else "ok",
        )
    )
