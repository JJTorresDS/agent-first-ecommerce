from .models import AppAccess, user_can_open


def nav(request):
    user = getattr(request, "user", None)
    return {
        "can_open_cashflow": user_can_open(user, AppAccess.App.CASHFLOW),
        "can_grant_access": bool(getattr(user, "is_superuser", False)),
    }
