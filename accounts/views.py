from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .models import (
    AppAccess,
    Membership,
    Profile,
    create_owned_organization,
    membership_for,
)


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
    if membership_for(request.user):
        return redirect("organization")
    error = ""
    if request.method == "POST":
        name = _organization_name(request.POST.get("name"))
        if not name:
            error = "Enter an organization name."
        else:
            create_owned_organization(request.user, name)
            return redirect("organization")
    return render(request, "accounts/organization_setup.html", {"error": error})


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
        {
            "organization": organization,
            "member_count": organization.memberships.count(),
            "error": error,
        },
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
            {
                "organization": membership.organization,
                "member_count": membership.organization.memberships.count(),
                "error": "Confirm the deletion to remove the organization.",
            },
        )
    membership.organization.delete()
    return redirect("organization_setup")
