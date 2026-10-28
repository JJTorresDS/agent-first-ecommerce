from allauth.account.signals import user_signed_up
from django.dispatch import receiver

from .models import Profile


@receiver(user_signed_up)
def copy_google_profile(request, user, sociallogin=None, **kwargs):
    if sociallogin is None or sociallogin.account.provider != "google":
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
