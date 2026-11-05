from notifications.registry import groups_for

from .models import (
    IMPERSONATOR_SESSION_KEY,
    AppAccess,
    Profile,
    can_manage_members,
    membership_for,
    memberships_for,
    apps_for,
)


def nav(request):
    if getattr(request, "inbox_partial", False):
        return {}
    user = getattr(request, "user", None)
    membership = membership_for(user)
    name, initials, avatar = _identity(user)
    organization = membership.organization if membership else None
    user_memberships = []
    other_unread = 0
    impersonating = False
    impersonated_label = ""
    waiting = {}
    if getattr(user, "is_authenticated", False):
        # A copy: the sort below must not reorder the list membership_for reads.
        user_memberships = list(memberships_for(user))
        from inbox.unread import unread_by_organization, waiting_counts

        waiting = waiting_counts(user)
        unread = unread_by_organization(user, waiting)
        for item in user_memberships:
            item.unread = unread.get(item.organization_id, 0)
            item.active = organization is not None and item.organization_id == organization.id
        # The active organization first, then the rest by name.
        user_memberships.sort(key=lambda item: not item.active)
        other_unread = sum(item.unread for item in user_memberships if not item.active)
        if request.session.get(IMPERSONATOR_SESSION_KEY):
            impersonating = True
            impersonated_label = user.email or user.get_username()
    groups = groups_for(organization, waiting.get(organization.id, {}) if organization else None)
    apps = apps_for(user, membership)
    return {
        "can_open_cashflow": AppAccess.App.CASHFLOW in apps,
        "can_open_catalog": AppAccess.App.CATALOG in apps,
        "can_open_purchasing": AppAccess.App.PURCHASING in apps,
        "can_grant_access": bool(getattr(user, "is_superuser", False)),
        "can_manage_members": can_manage_members(membership),
        "impersonating": impersonating,
        "impersonated_label": impersonated_label,
        "user_memberships": user_memberships,
        "other_unread": other_unread,
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
