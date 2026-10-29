from datetime import date

from django.http import HttpResponse
from django.shortcuts import render

from accounts.access import require_app
from accounts.models import AppAccess

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


@require_app(AppAccess.App.CASHFLOW)
def cashflow_list(request):
    category = request.GET.get("category") or ""
    currency = request.GET.get("currency") or ""
    raw_date = request.GET.get("transaction_date") or ""
    chosen = _chosen_date(raw_date)
    movements = CashflowMovement.objects.all()
    if category:
        movements = movements.filter(category=category)
    if currency:
        movements = movements.filter(currency=currency)
    if chosen:
        movements = movements.filter(transaction_date=chosen)
    context = {
        "movements": movements,
        "categories": CashflowMovement.objects.order_by("category")
        .values_list("category", flat=True)
        .distinct(),
        "currencies": CashflowMovement.objects.order_by("currency")
        .values_list("currency", flat=True)
        .distinct(),
        "category": category,
        "currency": currency,
        "transaction_date": raw_date if chosen else "",
    }
    template = (
        "cashflow/_table.html"
        if request.headers.get("HX-Request")
        else "cashflow/list.html"
    )
    return render(request, template, context)
