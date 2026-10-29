from allauth.account.signals import user_signed_up
from django.dispatch import receiver

from .models import Membership, Profile


def _is_google_signup(sociallogin):
    return sociallogin is not None and sociallogin.account.provider == "google"


@receiver(user_signed_up)
def copy_google_profile(request, user, sociallogin=None, **kwargs):
    if not _is_google_signup(sociallogin):
        return
    membership = (
        Membership.objects.filter(user=user).select_related("organization").first()
    )
    if membership is None:
        return
    extra = sociallogin.account.extra_data or {}
    profile, _ = Profile.objects.get_or_create(
        user=user,
        defaults={"organization": membership.organization},
    )
    name = (extra.get("name") or "").strip()
    picture = (extra.get("picture") or "").strip()
    if name:
        profile.display_name = name
    if picture:
        profile.avatar_url = picture
    profile.save()
