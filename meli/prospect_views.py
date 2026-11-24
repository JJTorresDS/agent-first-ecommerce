"""Prospects (Mercado Libre → Prospects): listings scraped from Mercado Libre, sent by POST /api/meli/prospects.

Link one to a catalog product, then Create listing opens a regular draft for that product, which copies the
prospect's characteristics once its category's attributes load.
"""

from django.contrib import messages
from django.db.models import Q
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from accounts.access import require_app
from accounts.models import AppAccess
from vimple import deletion

from .draft_views import READ_ONLY, _listable, _scope, product_draft
from .drafts import inventory_started, stock_by_product
from .models import MercadoLibreAccount, MeliListingProspect

Status = MeliListingProspect.Status
SHOWN = [(Status.PENDING, "Pending"), (Status.DRAFTED, "Draft started"), (Status.DISMISSED, "Dismissed"), ("all", "All")]
PAGE = 200


def _back(request):
    back = request.POST.get("next") or ""
    if not url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        back = reverse("meli_prospects")
    return back


def _link(request, organization, prospect):
    """Link the prospect to the product with the typed SKU, in this organization; a blank SKU unlinks it."""
    from catalog.models import MasterProduct

    sku = request.POST.get("sku", "").strip()
    if not sku:
        prospect.product = None
    else:
        product = (
            MasterProduct.objects.filter(organization=organization, sku__iexact=sku)
            .exclude(status=MasterProduct.Status.ARCHIVED)
            .first()
        )
        if product is None:
            messages.warning(request, f"No product with SKU {sku}.")
            return
        prospect.product = product
    prospect.save(update_fields=["product", "updated_at"])


@require_app(AppAccess.App.CATALOG)
def meli_prospects(request):
    from catalog.models import MasterProduct

    organization, can_edit = _scope(request)
    rows = MeliListingProspect.objects.filter(organization=organization)
    if request.method == "POST":
        if not can_edit:
            return HttpResponseForbidden(READ_ONLY)
        action = request.POST.get("action")
        if action in ("dismiss_selected", "restore_selected", "delete_selected"):
            chosen = deletion.selected(request, rows)
            if action == "delete_selected":
                rows.filter(pk__in=[row.pk for row in chosen]).delete()
                messages.success(request, f"{len(chosen)} deleted.")
            else:
                status = Status.DISMISSED if action == "dismiss_selected" else Status.PENDING
                rows.filter(pk__in=[row.pk for row in chosen]).update(status=status)
            return redirect(_back(request))
        prospect = get_object_or_404(rows, pk=request.POST.get("prospect"))
        if action == "link":
            _link(request, organization, prospect)
        elif action in ("dismiss", "restore"):
            prospect.status = Status.DISMISSED if action == "dismiss" else (
                Status.DRAFTED if prospect.draft_id else Status.PENDING
            )
            prospect.save(update_fields=["status", "updated_at"])
        return redirect(_back(request))

    shown = request.GET.get("status") if request.GET.get("status") in dict(SHOWN) else Status.PENDING
    query = request.GET.get("q", "").strip()
    if shown != "all":
        rows = rows.filter(status=shown)
    if query:
        rows = rows.filter(Q(title__icontains=query) | Q(external_id__icontains=query) | Q(seller__icontains=query))
    prospects = list(rows.select_related("product", "draft")[:PAGE + 1])
    more = len(prospects) > PAGE
    prospects = prospects[:PAGE]
    # Read once for every row: _listable says why a linked product can't get a listing yet.
    stock = stock_by_product(organization)
    connected = MercadoLibreAccount.objects.filter(organization=organization).exists()
    started = inventory_started(organization)
    for prospect in prospects:
        prospect.reason = (
            _listable(organization, prospect.product, stock=stock, connected=connected, started=started)
            if prospect.product else ""
        )
    products = (
        MasterProduct.objects.filter(organization=organization)
        .exclude(status=MasterProduct.Status.ARCHIVED)
        .order_by("sku")
        .values_list("sku", "title")
        if can_edit else []
    )
    return render(
        request,
        "meli/prospects.html",
        {
            "prospects": prospects,
            "more": more,
            "shown": shown,
            "shown_choices": SHOWN,
            "query": query,
            "filtered": bool(query) or shown != Status.PENDING,
            "any": prospects or MeliListingProspect.objects.filter(organization=organization).exists(),
            "products": products,
            "can_edit": can_edit,
            "back": request.get_full_path(),
            "bulk": deletion.bulk(
                "prospects",
                ("dismiss_selected", "Dismiss", "", False),
                ("restore_selected", "Back to pending", "", False),
                ("delete_selected", "Delete", "Delete {n} selected? A new scrape brings them back.", True),
            ) if can_edit else None,
            "active_tab": "meli_prospects",
        },
    )


@require_app(AppAccess.App.CATALOG)
@require_POST
def meli_draft_from_prospect(request, pk):
    """A regular draft for the prospect's product; its page then copies the prospect's characteristics."""
    organization, can_edit = _scope(request)
    if not can_edit:
        return HttpResponseForbidden(READ_ONLY)
    prospect = get_object_or_404(
        MeliListingProspect.objects.select_related("product"), organization=organization, pk=pk, product__isnull=False
    )
    reason = _listable(organization, prospect.product)
    if reason:
        messages.warning(request, f"{prospect.product.sku}: {reason}")
        return redirect(_back(request))
    draft = product_draft(organization, prospect.product, request.user)
    prospect.draft = draft
    prospect.status = Status.DRAFTED
    prospect.save(update_fields=["draft", "status", "updated_at"])
    return redirect(f"{reverse('meli_draft', args=[draft.pk])}?prospect={prospect.pk}")
