import os

from django.db import transaction
from django.utils import timezone

from .models import ErrorReport


def app_version():
    """The deployed commit on Render; blank on a laptop."""
    return os.environ.get("RENDER_GIT_COMMIT", "")[:40]


@transaction.atomic
def report(*, organization, user, source, object_id, title, details, context, note=""):
    """Store a report, or add to the open one for the same problem. Returns (report, created)."""
    response = details.get("response") if isinstance(details.get("response"), dict) else {}
    causes = [cause for cause in response.get("cause") or [] if isinstance(cause, dict)]
    cause_codes = [str(cause.get("code") or "") for cause in causes if cause.get("code")]
    error_code = str(response.get("error") or "")[:100]
    call = str(details.get("call") or "")[:200]
    fingerprint = ErrorReport.fingerprint_of(source, object_id, call, error_code, cause_codes)
    fields = {
        "title": (title or "")[:500],
        "details": details,
        "context": {**context, "app_version": app_version(), "reported_at": timezone.now().isoformat()},
        "reported_by": user if getattr(user, "is_authenticated", False) else None,
    }
    existing = (
        ErrorReport.objects.select_for_update()
        .filter(organization=organization, fingerprint=fingerprint, status=ErrorReport.Status.OPEN)
        .first()
    )
    if existing is not None:
        for name, value in fields.items():
            setattr(existing, name, value)
        if note:
            existing.note = (f"{existing.note}\n---\n{note}" if existing.note else note)[:2000]
        existing.occurrences += 1
        existing.save()
        return existing, False
    created = ErrorReport.objects.create(
        organization=organization,
        source=source,
        object_id=str(object_id),
        call=call,
        http_status=details.get("status") if isinstance(details.get("status"), int) else None,
        error_code=error_code,
        cause_codes=cause_codes,
        fingerprint=fingerprint,
        note=(note or "")[:2000],
        **fields,
    )
    return created, True
