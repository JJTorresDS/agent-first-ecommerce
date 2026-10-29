from allauth.account.signals import user_signed_up
from django.contrib.auth import get_user_model
from django.dispatch import receiver

from tasks.models import Task

from .models import Profile


def _is_google_signup(sociallogin):
    return sociallogin is not None and sociallogin.account.provider == "google"


@receiver(user_signed_up)
def copy_google_profile(request, user, sociallogin=None, **kwargs):
    if not _is_google_signup(sociallogin):
        return
    extra = sociallogin.account.extra_data or {}
    profile, _ = Profile.objects.get_or_create(user=user)
    name = (extra.get("name") or "").strip()
    picture = (extra.get("picture") or "").strip()
    if name:
        profile.display_name = name
    if picture:
        profile.avatar_url = picture
    profile.save()


@receiver(user_signed_up)
def assign_access_task(request, user, sociallogin=None, **kwargs):
    if not _is_google_signup(sociallogin):
        return
    admin = (
        get_user_model().objects.filter(is_superuser=True).order_by("pk").first()
    )
    who = user.email or user.get_username()
    Task.objects.create(
        email_address=who,
        task_title=f"Grant access to {who}",
        assignee=admin.email if admin and admin.email else "",
        status=Task.Status.TODO,
    )
