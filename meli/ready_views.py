"""Fill in what a product still needs before it can be listed (price, package, Depósito stock),
from a dialog on the draft page or the scraped listings tab, without leaving it.

GET answers the dialog's form; POST saves to the product (stock as a Depósito count through the
ledger) and answers 204: HX-Refresh on the scraped tab, HX-Trigger product-ready on a draft page,
which keeps the draft's unsaved fields.
"""

import json

from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, render

from accounts.access import require_app
from accounts.models import AppAccess
from catalog.forms import TenantForm, _require_price
from catalog.models import PACKAGE_FIELDS, MasterProduct, package_missing

from .draft_views import READ_ONLY, _scope
from .drafts import inventory_started, stock_by_product


def gaps(organization, product, stock=None, started=None):
    """What this product still needs before it can be listed: price, package fields, stock.

    stock and started are read once when a list of rows asks.
    """
    if stock is None:
        stock = stock_by_product(organization)
    if started is None:
        started = inventory_started(organization)
    units = stock.get(product.pk, 0)
    return {
        "price": not product.list_price,
        "package": package_missing(product),
        "stock": not started or units < 1,
        "first_count": not started,
        "units": units,
    }


def needs_anything(found):
    return found["price"] or bool(found["package"]) or found["stock"]


class ReadyForm(TenantForm):
    """The product's price (when it has none), its package, and a Depósito count (when it has no stock)."""

    units = forms.IntegerField(label="Units in the Depósito", min_value=1, required=False)

    class Meta:
        model = MasterProduct
        fields = ["list_price", *PACKAGE_FIELDS]

    def __init__(self, *args, found, **kwargs):
        super().__init__(*args, **kwargs)
        self.found = found
        if found["price"]:
            _require_price(self.fields["list_price"])
        else:
            del self.fields["list_price"]
        for name in PACKAGE_FIELDS:
            # Mercado Libre needs all four on every listing.
            self.fields[name].required = True
            step = "1" if name == "weight_g" else "0.1"
            self.fields[name].widget.attrs.update({"min": step, "step": step})
        if found["stock"]:
            self.fields["units"].required = True
            self.fields["units"].help_text = "What you have on the shelf now. Saved as a stock count."
        else:
            del self.fields["units"]


@require_app(AppAccess.App.CATALOG)
def meli_product_ready(request, pk):
    from inventory import ledger

    organization, can_edit = _scope(request)
    if not can_edit:
        return HttpResponseForbidden(READ_ONLY)
    product = get_object_or_404(MasterProduct, organization=organization, pk=pk)
    place = "draft" if request.GET.get("place", request.POST.get("place")) == "draft" else "list"
    found = gaps(organization, product)
    form = ReadyForm(request.POST or None, instance=product, found=found)
    if request.method == "POST" and form.is_valid():
        by = request.user.email or request.user.get_username()
        try:
            with transaction.atomic():
                form.save()
                units = form.cleaned_data.get("units")
                if units:
                    opening = ledger.start(organization, by=by)
                    ledger.count(organization, product, ledger.OWN, units, opening=opening, by=by)
                    if opening:
                        ledger.apply_pending_sales(organization)
        except ValidationError as failure:
            form.add_error(None, failure)
        else:
            if place == "list":
                # The scraped tab's rows read their reasons again.
                response = HttpResponse(status=204)
                response["HX-Refresh"] = "true"
                return response
            after = gaps(organization, product)
            response = HttpResponse(status=204)
            response["HX-Trigger"] = json.dumps(
                {"product-ready": {"dialog": "ready-dialog", "available": after["units"], "done": not needs_anything(after)}}
            )
            return response
    return render(
        request,
        "meli/_ready_form.html",
        {
            "product": product,
            "form": form,
            "found": found,
            "place": place,
            "package_rows": [(form[name], name in found["package"]) for name in PACKAGE_FIELDS],
        },
    )
