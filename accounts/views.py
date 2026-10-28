from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from .models import Profile


def home(request):
    if request.user.is_authenticated:
        return redirect("profile")
    return redirect("google_login")


@login_required
def profile(request):
    profile_row, _ = Profile.objects.get_or_create(user=request.user)
    return render(request, "accounts/profile.html", {"profile": profile_row})
