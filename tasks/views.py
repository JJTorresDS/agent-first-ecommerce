import csv
from datetime import date

from django.http import HttpResponse
from django.shortcuts import render

from accounts.access import require_app
from accounts.models import AppAccess, membership_for

from .models import CashflowMovement


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


def _filtered_movements(request):
    category = request.GET.get("category") or ""
    currency = request.GET.get("currency") or ""
    raw_date = request.GET.get("transaction_date") or ""
    chosen = _chosen_date(raw_date)
    organization = membership_for(request.user).organization
    movements = CashflowMovement.objects.filter(organization=organization)
    if category:
        movements = movements.filter(category=category)
    if currency:
        movements = movements.filter(currency=currency)
    if chosen:
        movements = movements.filter(transaction_date=chosen)
    return movements, {
        "category": category,
        "currency": currency,
        "transaction_date": raw_date if chosen else "",
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
    context = {"movements": movements, **filters}
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
                movement.source_file_url(),
            ]
        )
    return response
