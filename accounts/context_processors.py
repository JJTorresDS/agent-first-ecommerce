from notifications.registry import groups_for

from .models import AppAccess, Profile, membership_for, user_can_open


def nav(request):
    user = getattr(request, "user", None)
    membership = membership_for(user)
    name, initials, avatar = _identity(user)
    organization = membership.organization if membership else None
    groups = groups_for(organization)
    return {
        "can_open_cashflow": user_can_open(user, AppAccess.App.CASHFLOW),
        "can_grant_access": bool(getattr(user, "is_superuser", False)),
        "organization": organization,
        "alert_groups": groups,
        "alert_count": sum(group["count"] for group in groups),
        "nav_name": name,
        "nav_initials": initials,
        "nav_avatar": avatar,
    }


def _identity(user):
    if not getattr(user, "is_authenticated", False):
        return "", "", ""
    profile = _profile(user)
    name = ""
    avatar = ""
    if profile is not None:
        name = profile.display_name
        avatar = profile.avatar_url
    if not name:
        name = user.email or user.get_username()
    return name, _initials(name), avatar


def _profile(user):
    try:
        return user.profile
    except Profile.DoesNotExist:
        return None


def _initials(name):
    words = [
        word
        for word in name.replace("@", " ").replace(".", " ").replace("_", " ").split()
        if word
    ]
    if not words:
        return ""
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()
