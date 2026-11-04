import re

from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

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
)

_AUTH_BACKEND = "django.contrib.auth.backends.ModelBackend"


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
        "tasks": AppAccess.App.TASKS in granted,
        "cashflow": AppAccess.App.CASHFLOW in granted,
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


@login_required
@require_GET
def impersonate(request, user_id):
    if request.session.get(IMPERSONATOR_SESSION_KEY):
        return HttpResponseForbidden("An impersonation is already active.")
    if not request.user.is_superuser:
        return HttpResponseForbidden(
            "Only a superuser can open Vimple as another user."
        )
    target = get_object_or_404(get_user_model(), pk=user_id)
    superuser_id = request.user.pk
    login(request, target, backend=_AUTH_BACKEND)
    request.session[IMPERSONATOR_SESSION_KEY] = superuser_id
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
    login(request, superuser, backend=_AUTH_BACKEND)
    request.session.pop(IMPERSONATOR_SESSION_KEY, None)
    return redirect("admin:index")


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
    return redirect(request.POST.get("next") or "organization")
