import csv
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from django.conf import settings
from django.core.paginator import Paginator
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from accounts.access import require_app
from accounts.models import AppAccess, can_manage_members, membership_for

from . import drive
from .models import FOLDER_SLOTS, CashflowMovement, CashflowParserSettings, folder_id
from .parser import start_parser

PAGE_SIZE = 25


def health(request):
    """Liveness check. Does not read or write the database."""
    return HttpResponse("ok", content_type="text/plain")


def _chosen_date(raw):
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _chosen_confidence(raw):
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    if value < 0 or value > 1:
        return None
    return value


def _filtered_movements(request):
    category = request.GET.get("category") or ""
    currency = request.GET.get("currency") or ""
    date_from = _chosen_date(request.GET.get("date_from") or "")
    date_to = _chosen_date(request.GET.get("date_to") or "")
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from
    confidence_from = _chosen_confidence(request.GET.get("amount_confidence_from") or "")
    confidence_to = _chosen_confidence(request.GET.get("amount_confidence_to") or "")
    if (
        confidence_from is not None
        and confidence_to is not None
        and confidence_from > confidence_to
    ):
        confidence_from, confidence_to = confidence_to, confidence_from
    organization = membership_for(request.user).organization
    movements = CashflowMovement.objects.filter(organization=organization)
    if category:
        movements = movements.filter(category=category)
    if currency:
        movements = movements.filter(currency=currency)
    if date_from:
        movements = movements.filter(transaction_date__gte=date_from)
    if date_to:
        movements = movements.filter(transaction_date__lte=date_to)
    if confidence_from is not None:
        movements = movements.filter(amount_parsing_confidence__gte=confidence_from)
    if confidence_to is not None:
        movements = movements.filter(amount_parsing_confidence__lte=confidence_to)
    filters = {
        "category": category,
        "currency": currency,
        "date_from": date_from.isoformat() if date_from else "",
        "date_to": date_to.isoformat() if date_to else "",
        "amount_confidence_from": f"{confidence_from:.3f}" if confidence_from is not None else "",
        "amount_confidence_to": f"{confidence_to:.3f}" if confidence_to is not None else "",
    }
    filters["filter_query"] = urlencode(filters)
    return movements, {
        **filters,
        "categories": CashflowMovement.objects.filter(organization=organization)
        .order_by("category")
        .values_list("category", flat=True)
        .distinct(),
        "currencies": CashflowMovement.objects.filter(organization=organization)
        .order_by("currency")
        .values_list("currency", flat=True)
        .distinct(),
    }


@require_app(AppAccess.App.CASHFLOW)
def cashflow_list(request):
    movements, filters = _filtered_movements(request)
    page = Paginator(movements, PAGE_SIZE).get_page(request.GET.get("page") or 1)
    context = {"movements": page.object_list, "page_obj": page, **filters}
    template = (
        "cashflow/_results.html"
        if request.headers.get("HX-Request")
        else "cashflow/list.html"
    )
    return render(request, template, context)


@require_app(AppAccess.App.CASHFLOW)
def cashflow_csv(request):
    movements, _filters = _filtered_movements(request)
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="cashflow.csv"'
    writer = csv.writer(response)
    writer.writerow(
        [
            "Date",
            "Category",
            "Subcategory",
            "Amount",
            "Currency",
            "Counterparty",
            "Description",
            "White",
            "Receipt",
            "Document type",
            "Amount confidence",
            "Observations",
            "Document",
        ]
    )
    for movement in movements:
        writer.writerow(
            [
                movement.transaction_date.isoformat(),
                movement.category,
                movement.sub_category or "",
                movement.amount,
                movement.currency,
                movement.counterparty_name,
                movement.description,
                "Yes" if movement.white else "No",
                movement.receipt_id,
                movement.document_type,
                "" if movement.amount_parsing_confidence is None else movement.amount_parsing_confidence,
                movement.parsing_observations_text(),
                movement.source_file_url(),
            ]
        )
    return response


RUN_ERRORS = {
    "nokey": "The receipt parser key is not set on the server.",
    "missing": "Choose every folder before running the parser.",
}


def _parser_settings(organization):
    parser_settings, _created = CashflowParserSettings.objects.get_or_create(
        organization=organization
    )
    return parser_settings


def _settings_context(request, membership, parser_settings, errors=None, values=None):
    errors = errors or {}
    values = values or {}
    can_edit = can_manage_members(membership)
    rows = []
    for slot, label in FOLDER_SLOTS:
        saved = parser_settings.folder(slot)
        rows.append(
            {
                "slot": slot,
                "label": label,
                "id": values.get(slot, saved["id"]),
                "name": saved["name"],
                "error": errors.get(slot, ""),
            }
        )
    picker = can_edit and drive.picker_enabled()
    connected = picker and drive.drive_connected(request.user)
    return {
        "rows": rows,
        "parser_settings": parser_settings,
        "can_edit": can_edit,
        "picker": picker,
        "drive_connected": connected,
        "drive_connect_url": (
            drive.connect_url(request.user, reverse("cashflow_settings"))
            if picker and not connected
            else ""
        ),
        "picker_api_key": settings.GOOGLE_PICKER_API_KEY if picker else "",
        "picker_app_id": settings.GOOGLE_CLOUD_PROJECT_NUMBER if picker else "",
        "run_error": RUN_ERRORS.get(request.GET.get("run", ""), ""),
        "missing": parser_settings.missing_slots(),
        "saved": request.GET.get("saved") == "1",
    }


@require_app(AppAccess.App.CASHFLOW)
def cashflow_settings(request):
    membership = membership_for(request.user)
    if membership is None:
        return redirect("organization_setup")
    parser_settings = _parser_settings(membership.organization)
    if request.headers.get("HX-Request") and request.method == "GET":
        context = _settings_context(request, membership, parser_settings)
        return render(request, "cashflow/_parser_status.html", context)
    if request.method == "POST":
        if not can_manage_members(membership):
            return HttpResponseForbidden()
        folders = {}
        errors = {}
        values = {}
        for slot, _label in FOLDER_SLOTS:
            raw = request.POST.get(slot, "").strip()
            values[slot] = raw
            chosen = folder_id(raw)
            if raw and not chosen:
                errors[slot] = "Not a Drive folder ID or link."
                continue
            if not chosen:
                continue
            saved = parser_settings.folder(slot)
            name = request.POST.get(f"{slot}__name", "").strip()[:200]
            if chosen == saved["id"] and not name:
                name = saved["name"]
            folders[slot] = {"id": chosen, "name": name}
        if errors:
            context = _settings_context(request, membership, parser_settings, errors, values)
            return render(request, "cashflow/settings.html", context, status=400)
        parser_settings.folders = folders
        parser_settings.save(update_fields=["folders", "updated_at"])
        return redirect(f"{reverse('cashflow_settings')}?saved=1")
    context = _settings_context(request, membership, parser_settings)
    return render(request, "cashflow/settings.html", context)


@require_app(AppAccess.App.CASHFLOW)
@require_POST
def cashflow_parser_run(request):
    membership = membership_for(request.user)
    if membership is None:
        return redirect("organization_setup")
    if not can_manage_members(membership):
        return HttpResponseForbidden()
    error = start_parser(_parser_settings(membership.organization), request.user)
    location = reverse("cashflow_settings")
    if error:
        location += "?" + urlencode({"run": error})
    return redirect(location)


@require_app(AppAccess.App.CASHFLOW)
@require_POST
def cashflow_drive_token(request):
    """The signed-in admin's own Drive token for the Google Picker."""
    membership = membership_for(request.user)
    if membership is None or not can_manage_members(membership):
        return HttpResponseForbidden()
    try:
        token = drive.access_token(request.user)
    except (OSError, ValueError):
        token = ""
    response = JsonResponse({"access_token": token}, status=200 if token else 409)
    response["Cache-Control"] = "no-store"
    return response
