from notifications.registry import groups_for

from .models import (
    IMPERSONATOR_SESSION_KEY,
    AppAccess,
    Membership,
    Profile,
    can_manage_members,
    membership_for,
    user_can_open,
)


def nav(request):
    if getattr(request, "inbox_partial", False):
        return {}
    user = getattr(request, "user", None)
    membership = membership_for(user)
    name, initials, avatar = _identity(user)
    organization = membership.organization if membership else None
    groups = groups_for(organization)
    user_memberships = []
    impersonating = False
    impersonated_label = ""
    if getattr(user, "is_authenticated", False):
        user_memberships = list(
            Membership.objects.filter(user=user)
            .select_related("organization")
            .order_by("organization__name", "pk")
        )
        if request.session.get(IMPERSONATOR_SESSION_KEY):
            impersonating = True
            impersonated_label = user.email or user.get_username()
    return {
        "can_open_cashflow": user_can_open(user, AppAccess.App.CASHFLOW),
        "can_grant_access": bool(getattr(user, "is_superuser", False)),
        "can_manage_members": can_manage_members(membership),
        "impersonating": impersonating,
        "impersonated_label": impersonated_label,
        "user_memberships": user_memberships,
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
