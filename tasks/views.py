from django.http import HttpResponse


def health(request):
    """Liveness check. Does not read or write the database."""
    return HttpResponse("ok", content_type="text/plain")
