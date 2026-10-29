from django.shortcuts import redirect

from .models import Membership

_EXEMPT_PREFIXES = (
    "/organization/setup/",
    "/accounts/",
    "/admin/",
    "/health/",
    "/api/",
)


class OrganizationRequiredMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            request.user.is_authenticated
            and not request.path.startswith(_EXEMPT_PREFIXES)
            and not Membership.objects.filter(user=request.user).exists()
        ):
            return redirect("organization_setup")
        return self.get_response(request)
