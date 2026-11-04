import json
import logging
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from django.conf import settings
from django.db import close_old_connections, transaction
from django.utils import timezone

from .models import FOLDER_SLOTS, CashflowParserSettings

logger = logging.getLogger(__name__)

RECEIPT_PARSER_URL = "https://workflows.pyrolabs.org/webhook/receipt-parserV2"
TIMEOUT_SECONDS = 30
TRIES = 3
WAIT_SECONDS = 5


def build_payload(organization, parser_settings):
    """The body the n8n trigger workflow sends to receipt-parserV2."""
    config = {
        "organization_id": str(organization.pk),
        "organization_name": organization.name,
    }
    for slot, _label in FOLDER_SLOTS:
        value = parser_settings.folder(slot)["id"]
        if "." in slot:
            group, key = slot.split(".", 1)
            config.setdefault(group, {})[key] = value
        else:
            config[slot] = value
    return {"organization_id": str(organization.pk), "files": [config]}


def start_parser(parser_settings, user):
    """Mark the run as started and send it in the background.

    Returns an error code (nokey, missing), or an empty string when the run started.
    """
    if not settings.RECEIPT_PARSER_API_KEY:
        return "nokey"
    if parser_settings.missing_slots():
        return "missing"
    with transaction.atomic():
        locked = CashflowParserSettings.objects.select_for_update().get(
            pk=parser_settings.pk,
            organization_id=parser_settings.organization_id,
        )
        if locked.last_run_status == CashflowParserSettings.Status.RUNNING:
            return ""
        locked.last_run_status = CashflowParserSettings.Status.RUNNING
        locked.last_run_error = ""
        locked.last_run_at = timezone.now()
        locked.last_run_by = getattr(user, "email", "") or ""
        locked.save(
            update_fields=[
                "last_run_status",
                "last_run_error",
                "last_run_at",
                "last_run_by",
                "updated_at",
            ]
        )
    if _under_test():
        run_parser(parser_settings.pk)
    else:
        threading.Thread(
            target=run_parser,
            args=(parser_settings.pk,),
            name="receipt-parser",
            daemon=True,
        ).start()
    return ""


def run_parser(settings_pk):
    close_old_connections()
    try:
        parser_settings = CashflowParserSettings.objects.select_related(
            "organization"
        ).get(pk=settings_pk)
        body = json.dumps(
            build_payload(parser_settings.organization, parser_settings)
        ).encode()
        error = ""
        for attempt in range(TRIES):
            error = _send(body)
            if not error:
                break
            if attempt < TRIES - 1:
                time.sleep(WAIT_SECONDS)
        CashflowParserSettings.objects.filter(pk=settings_pk).update(
            last_run_status=(
                CashflowParserSettings.Status.FAILED
                if error
                else CashflowParserSettings.Status.OK
            ),
            last_run_error=error[:200],
            updated_at=timezone.now(),
        )
    except Exception:
        logger.exception("Receipt parser run failed for %s", settings_pk)
        CashflowParserSettings.objects.filter(pk=settings_pk).update(
            last_run_status=CashflowParserSettings.Status.FAILED,
            last_run_error="The receipt parser could not be started.",
        )
    finally:
        close_old_connections()


def _send(body):
    """POST once. Returns an error message, or an empty string on success."""
    request = Request(
        RECEIPT_PARSER_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-API-Key": settings.RECEIPT_PARSER_API_KEY,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            response.read()
    except HTTPError as error:
        return f"The receipt parser answered {error.code}."
    except OSError:
        return "The receipt parser could not be reached."
    return ""


def _under_test():
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    return command == "test"
