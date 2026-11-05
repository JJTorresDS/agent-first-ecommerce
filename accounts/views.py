import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone as dt_timezone

from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from .models import (
    IMPERSONATOR_SESSION_KEY,
    AppAccess,
    Invitation,
    Membership,
    Profile,
    add_organization_member,
    can_manage_members,
    create_owned_organization,
    ensure_profile,
    membership_for,
    memberships_for,
)

_AUTH_BACKEND = "django.contrib.auth.backends.ModelBackend"
logger = logging.getLogger(__name__)


def home(request):
    if request.user.is_authenticated:
        return redirect("profile")
    return redirect("google_login")


@login_required
def profile(request):
    membership = membership_for(request.user)
    if membership is None:
        return redirect("organization_setup")
    profile_row, _ = Profile.objects.get_or_create(
        user=request.user,
        defaults={"organization": membership.organization},
    )
    return render(request, "accounts/profile.html", {"profile": profile_row})


@login_required
def profile_menu(request):
    return render(request, "accounts/_profile_menu.html")


@login_required
@require_http_methods(["GET"])
def profile_unread(request):
    """Chats waiting for a reply in each of the user's organizations, for the avatar badge and the chime.

    Answers 204 when X-Unread-Version matches, so an unchanged poll sends nothing. With Redis the
    version comes from the organizations' change marks, so that 204 reads no chats from Postgres.
    since (epoch milliseconds, the last latest this tab saw) asks how many messages came after it,
    so each one chimes.
    """
    from inbox.unread import latest_incoming, unread_by_organization
    from meli.jobs import unread_marks

    membership = membership_for(request.user)
    active = membership.organization_id if membership else None
    organization_ids = sorted(str(item.organization_id) for item in memberships_for(request.user))
    marks = unread_marks(organization_ids)
    version = ""
    if marks is not None:
        # Each minute too, in case a write missed its mark.
        raw = json.dumps([str(active), organization_ids, marks, int(time.time() // 60)])
        version = "r" + hashlib.sha1(raw.encode()).hexdigest()[:15]
        if request.headers.get("X-Unread-Version") == version:
            return HttpResponse(status=204)
    counts = unread_by_organization(request.user)
    newest, new = latest_incoming(request.user, _since(request.GET.get("since")))
    # Milliseconds since 1970: browsers compare numbers; Safari can't parse microsecond ISO dates.
    latest = int(newest.timestamp() * 1000) if newest else 0
    organizations = {str(key): value for key, value in sorted(counts.items(), key=lambda pair: str(pair[0]))}
    if not version:
        version = hashlib.sha1(json.dumps([str(active), organizations, latest]).encode()).hexdigest()[:16]
        if request.headers.get("X-Unread-Version") == version:
            return HttpResponse(status=204)
    response = JsonResponse(
        {
            "total": sum(counts.values()),
            "other": sum(value for key, value in counts.items() if key != active),
            "organizations": organizations,
            "latest": latest,
            "new": new,
            "version": version,
        }
    )
    response["Cache-Control"] = "no-store"
    return response


def _since(raw):
    """The moment after a latest the browser saw (epoch milliseconds), or None.

    latest drops the microseconds, so the message it came from would count again at that exact millisecond.
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return datetime.fromtimestamp((value + 1) / 1000, tz=dt_timezone.utc)


def _access_row(user):
    membership = membership_for(user)
    if membership is None:
        granted = set()
    else:
        granted = set(
            user.app_access.filter(organization=membership.organization).values_list(
                "app", flat=True
            )
        )
    return {
        "user": user,
        "organization": membership.organization if membership else None,
        "viewable": user.is_active and not user.is_superuser,
        "tasks": AppAccess.App.TASKS in granted,
        "cashflow": AppAccess.App.CASHFLOW in granted,
        "catalog": AppAccess.App.CATALOG in granted,
        "purchasing": AppAccess.App.PURCHASING in granted,
    }


@login_required
def access(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Only an admin can grant access.")
    users = get_user_model().objects.order_by("email", "username")
    return render(
        request,
        "accounts/access.html",
        {"rows": [_access_row(user) for user in users]},
    )


@login_required
@require_POST
def set_app_access(request, user_id):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Only an admin can grant access.")
    user = get_object_or_404(get_user_model(), pk=user_id)
    app = request.POST.get("app", "")
    if app not in AppAccess.App.values:
        return HttpResponseBadRequest("Unknown app.")
    membership = membership_for(user)
    if membership is None:
        return HttpResponseBadRequest("That user has no organization.")
    if request.POST.get("enabled") == "on":
        AppAccess.objects.get_or_create(
            user=user,
            app=app,
            organization=membership.organization,
        )
    else:
        AppAccess.objects.filter(
            user=user,
            app=app,
            organization=membership.organization,
        ).delete()
    if request.headers.get("HX-Request"):
        return render(
            request,
            "accounts/_access_user.html",
            {"row": _access_row(user)},
        )
    return redirect("access")


def _organization_name(raw):
    name = (raw or "").strip()
    if not name or len(name) > 200:
        return ""
    return name


@login_required
def organization_setup(request):
    has_organization = membership_for(request.user) is not None
    error = ""
    if request.method == "POST":
        name = _organization_name(request.POST.get("name"))
        if not name:
            error = "Enter an organization name."
        else:
            create_owned_organization(request.user, name)
            return redirect("organization")
    return render(
        request,
        "accounts/organization_setup.html",
        {"error": error, "has_organization": has_organization},
    )


def _organization_context(membership, error=""):
    organization = membership.organization
    return {
        "organization": organization,
        "member_count": organization.memberships.count(),
        "error": error,
        "is_owner": membership.role == Membership.Role.OWNER,
    }


@login_required
def organization_settings(request):
    membership = membership_for(request.user)
    if membership is None:
        return redirect("organization_setup")
    if request.method == "POST" and membership.role != Membership.Role.OWNER:
        return HttpResponseForbidden("Only the owner can rename the organization.")
    error = ""
    organization = membership.organization
    if request.method == "POST":
        name = _organization_name(request.POST.get("name"))
        if not name:
            error = "Enter an organization name."
        else:
            organization.name = name
            organization.save(update_fields=["name", "updated_at"])
            return redirect("organization")
    return render(
        request,
        "accounts/organization.html",
        _organization_context(membership, error),
    )


@login_required
@require_POST
def organization_delete(request):
    membership = membership_for(request.user)
    if membership is None:
        return redirect("organization_setup")
    if membership.role != Membership.Role.OWNER:
        return HttpResponseForbidden("Only the owner can delete the organization.")
    if request.POST.get("confirm") != "yes":
        return render(
            request,
            "accounts/organization.html",
            _organization_context(
                membership,
                "Confirm the deletion to remove the organization.",
            ),
        )
    membership.organization.delete()
    return redirect("organization_setup")


def _member_emails(raw):
    found = []
    seen = set()
    for chunk in re.split(r"[\s,;]+", raw or ""):
        email = chunk.strip()
        key = email.lower()
        if email and key not in seen:
            seen.add(key)
            found.append(email)
    return found


def _members_context(membership, error=""):
    organization = membership.organization
    return {
        "organization": organization,
        "members": organization.memberships.select_related("user").order_by(
            "user__email",
            "pk",
        ),
        "invitations": organization.invitations.order_by("email", "pk"),
        "error": error,
        "owner_role": Membership.Role.OWNER,
    }


@login_required
def members(request):
    membership = membership_for(request.user)
    if membership is None:
        return redirect("organization_setup")
    if not can_manage_members(membership):
        return HttpResponseForbidden("Only an owner or admin can manage members.")
    organization = membership.organization
    error = ""
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "add":
            emails = _member_emails(request.POST.get("emails"))
            role = request.POST.get("role")
            if not emails:
                error = "Enter an email address."
            elif role not in {Membership.Role.ADMIN, Membership.Role.CONTRIBUTOR}:
                error = "Choose admin or contributor."
            else:
                for email in emails:
                    add_organization_member(organization, email, role)
        elif action in {"role", "remove"}:
            target = get_object_or_404(
                Membership,
                pk=request.POST.get("membership"),
                organization=organization,
            )
            if target.role == Membership.Role.OWNER:
                error = "The owner stays the owner."
            elif action == "remove":
                target.delete()
            else:
                role = request.POST.get("role")
                if role not in {Membership.Role.ADMIN, Membership.Role.CONTRIBUTOR}:
                    error = "Choose admin or contributor."
                else:
                    target.role = role
                    target.save(update_fields=["role"])
        elif action == "withdraw":
            invitation = get_object_or_404(
                Invitation,
                pk=request.POST.get("invitation"),
                organization=organization,
            )
            invitation.delete()
        if not error:
            return redirect("members")
    return render(
        request,
        "accounts/members.html",
        _members_context(membership, error),
    )


IMPERSONATE_RETURN_KEY = "impersonate_return"
_RETURN_PLACES = {"access": "access", "admin": "admin:index"}


@login_required
@require_http_methods(["GET", "POST"])
def impersonate(request, user_id):
    """View Vimple as another user. A GET only asks; the switch is a POST, so a link can't trigger it."""
    if request.session.get(IMPERSONATOR_SESSION_KEY):
        return HttpResponseForbidden("An impersonation is already active.")
    if not request.user.is_superuser:
        return HttpResponseForbidden(
            "Only a superuser can open Vimple as another user."
        )
    target = get_object_or_404(get_user_model(), pk=user_id)
    if target.pk == request.user.pk or target.is_superuser or not target.is_active:
        return HttpResponseForbidden("Only an active user who is not a superuser can be viewed.")
    back = request.POST.get("back") or request.GET.get("back") or "admin"
    if back not in _RETURN_PLACES:
        back = "admin"
    if request.method == "GET":
        return render(
            request,
            "accounts/impersonate_confirm.html",
            {"target": target, "membership": membership_for(target), "back": back},
        )
    superuser_id = request.user.pk
    login(request, target, backend=_AUTH_BACKEND)
    # login() empties the session when the user changes, so write these after it.
    request.session[IMPERSONATOR_SESSION_KEY] = superuser_id
    request.session[IMPERSONATE_RETURN_KEY] = back
    logger.info("Superuser %s is viewing Vimple as user %s", superuser_id, target.pk)
    return redirect("profile")


@login_required
@require_POST
def impersonate_exit(request):
    superuser = get_user_model().objects.filter(
        pk=request.session.get(IMPERSONATOR_SESSION_KEY),
        is_superuser=True,
    ).first()
    if superuser is None:
        return HttpResponseForbidden("No impersonation is active.")
    back = request.session.get(IMPERSONATE_RETURN_KEY) or "admin"
    viewed = request.user.pk
    login(request, superuser, backend=_AUTH_BACKEND)
    request.session.pop(IMPERSONATOR_SESSION_KEY, None)
    request.session.pop(IMPERSONATE_RETURN_KEY, None)
    logger.info("Superuser %s stopped viewing Vimple as user %s", superuser.pk, viewed)
    return redirect(_RETURN_PLACES.get(back, "admin:index"))


_SWITCH_DESTINATIONS = {"home": "organization", "inbox": "inbox"}


@login_required
@require_POST
def organization_switch(request):
    chosen = Membership.objects.filter(
        user=request.user,
        organization_id=request.POST.get("organization"),
    ).select_related("organization").first()
    if chosen is None:
        return HttpResponseForbidden("That organization is not yours.")
    ensure_profile(request.user, chosen.organization)
    # Only these two places, never a URL from the request: no redirect to another site.
    return redirect(_SWITCH_DESTINATIONS.get(request.POST.get("go"), "organization"))
