from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .models import AppAccess, Profile


def home(request):
    if request.user.is_authenticated:
        return redirect("profile")
    return redirect("google_login")


@login_required
def profile(request):
    profile_row, _ = Profile.objects.get_or_create(user=request.user)
    return render(request, "accounts/profile.html", {"profile": profile_row})


def _access_row(user):
    granted = set(user.app_access.values_list("app", flat=True))
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
    if request.POST.get("enabled") == "on":
        AppAccess.objects.get_or_create(user=user, app=app)
    else:
        AppAccess.objects.filter(user=user, app=app).delete()
    if request.headers.get("HX-Request"):
        return render(
            request,
            "accounts/_access_user.html",
            {"row": _access_row(user)},
        )
    return redirect("access")
