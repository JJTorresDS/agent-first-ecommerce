from .models import AppAccess, membership_for, user_can_open


def nav(request):
    user = getattr(request, "user", None)
    membership = membership_for(user)
    return {
        "can_open_cashflow": user_can_open(user, AppAccess.App.CASHFLOW),
        "can_grant_access": bool(getattr(user, "is_superuser", False)),
        "organization": membership.organization if membership else None,
    }
