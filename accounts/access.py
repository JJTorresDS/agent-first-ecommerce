from functools import wraps

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from .models import AppAccess, user_can_open


def require_app(app):
    def decorator(view):
        @login_required
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if user_can_open(request.user, app):
                return view(request, *args, **kwargs)
            return render(
                request,
                "accounts/access_pending.html",
                {"app_label": AppAccess.App(app).label},
                status=403,
            )

        return wrapped

    return decorator
