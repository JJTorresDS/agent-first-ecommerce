import json
from datetime import datetime, timezone as dt_timezone
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.db import IntegrityError, transaction
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from accounts.models import Membership, membership_for

from cryptography.fernet import InvalidToken

from .client import exchange_code
from .models import MercadoLibreAccount, MercadoLibreNotification


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


def _store_object(parsed):
    notification_id = _clip(parsed.get("_id"), 128)
    user_id = _clip(parsed.get("user_id"), 64)
    fields = {
        "topic": _clip(parsed.get("topic"), 64),
        "resource": _clip(parsed.get("resource"), 512),
        "user_id": user_id,
        "application_id": _clip(parsed.get("application_id"), 64),
        "attempts": _attempts(parsed.get("attempts")),
        "sent_at": _when(parsed.get("sent")),
        "received_at": _when(parsed.get("received")),
        "payload": parsed,
        "organization": _organization_for(user_id),
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
def mercadolibre_webhook(request):
    raw = request.body.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        _store_object(parsed)
    else:
        MercadoLibreNotification.objects.create(payload=raw)
    return HttpResponse("ok")


def _owner_organization(request):
    membership = membership_for(request.user)
    if membership is None:
        return None, redirect("organization_setup")
    if membership.role != Membership.Role.OWNER:
        return None, HttpResponseForbidden(
            "Only the owner can connect Mercado Libre."
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
}


def _seller_snippet(access_token):
    return (
        "curl https://api.mercadolibre.com/users/me \\\n"
        f'  -H "Authorization: Bearer {access_token}"'
    )


def _settings_context(membership, meli_error="", access_token=""):
    return {
        "is_owner": membership.role == Membership.Role.OWNER,
        "meli_account": _account_for(membership.organization),
        "meli_error": meli_error,
        "access_token": access_token,
        "snippet": _seller_snippet(access_token) if access_token else "",
    }


@login_required
def mercadolibre_settings(request):
    membership = membership_for(request.user)
    if membership is None:
        return redirect("organization_setup")
    error = _MELI_ERRORS.get(request.GET.get("meli"), "")
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
    return _settings_redirect()


@login_required
@require_POST
def mercadolibre_disconnect(request):
    organization, denied = _owner_organization(request)
    if denied:
        return denied
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
