import json
from datetime import datetime, timezone as dt_timezone

from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import MercadoLibreNotification


def _clip(value, limit):
    if value is None:
        return ""
    return str(value)[:limit]


def _attempts(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _when(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, dt_timezone.utc)
    return parsed


def _store_object(parsed):
    notification_id = _clip(parsed.get("_id"), 128)
    fields = {
        "topic": _clip(parsed.get("topic"), 64),
        "resource": _clip(parsed.get("resource"), 512),
        "user_id": _clip(parsed.get("user_id"), 64),
        "application_id": _clip(parsed.get("application_id"), 64),
        "attempts": _attempts(parsed.get("attempts")),
        "sent_at": _when(parsed.get("sent")),
        "received_at": _when(parsed.get("received")),
        "payload": parsed,
    }
    if not notification_id:
        MercadoLibreNotification.objects.create(notification_id="", **fields)
        return
    try:
        with transaction.atomic():
            MercadoLibreNotification.objects.get_or_create(
                notification_id=notification_id,
                defaults=fields,
            )
    except IntegrityError:
        return


@csrf_exempt
@require_POST
def mercadolibre_webhook(request):
    raw = request.body.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        _store_object(parsed)
    else:
        MercadoLibreNotification.objects.create(payload=raw)
    return HttpResponse("ok")
