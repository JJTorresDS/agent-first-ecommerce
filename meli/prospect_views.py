"""Scraped listings (Create listing → From a scraped listing): Mercado Libre listings someone else sells,
sent by POST /api/meli/prospects (stored as MeliListingProspect).

Link one to a catalog product, then Create listing opens a regular draft for that product, which copies the
scraped listing's characteristics once its category's attributes load.
"""

from decimal import ROUND_FLOOR, Decimal

from django.contrib import messages
from django.db.models import Q
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from accounts.access import require_app
from accounts.models import AppAccess
from vimple import deletion

from .draft_views import READ_ONLY, _listable, _scope, product_draft
from .models import MeliListingProspect
from .prospects import category_in, request_categories

Status = MeliListingProspect.Status
SHOWN = [(Status.PENDING, "Pending"), (Status.DRAFTED, "Draft started"), (Status.DISMISSED, "Dismissed"), ("all", "All")]
PAGE = 200


def _back(request):
    back = request.POST.get("next") or ""
    if not url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        back = f"{reverse('meli_drafts')}?start=scraped"
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
    draft = prospect.draft
    if prospect.product and draft and draft.fast() and draft.item_id:
        # Its fast listing is on Mercado Libre: give it the product's SKU there, so the sync links it and its
        # sales come off the product's stock from now on (the ones waiting apply then too).
        from .jobs import dispatch_drafts_job

        dispatch_drafts_job(organization, "listing_sku", item_id=draft.item_id, sku=prospect.product.sku)
        messages.info(request, f"Linking {draft.item_id} to {prospect.product.sku} on Mercado Libre.")


def scraped_post(request, organization):
    """Create listing → From a scraped listing: link, dismiss, restore or delete. Owners and admins only (the caller checks)."""
    rows = MeliListingProspect.objects.filter(organization=organization)
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
    if action == "scraped_link":
        _link(request, organization, prospect)
    elif action == "scraped_category":
        # Look it up again: the scraper's category, else a new guess from the title.
        prospect.category_id = category_in(prospect.payload) if isinstance(prospect.payload, dict) else ""
        prospect.category_path, prospect.category_checked_at, prospect.category_error = "", None, ""
        prospect.save(update_fields=["category_id", "category_path", "category_checked_at", "category_error", "updated_at"])
        request_categories(organization, [prospect])
    else:
        prospect.status = Status.DISMISSED if action == "scraped_dismiss" else (
            Status.DRAFTED if prospect.draft_id else Status.PENDING
        )
        prospect.save(update_fields=["status", "updated_at"])
    return redirect(_back(request))


def scraped_context(request, organization, stock, connected, started):
    """The scraped listings for Create listing → From a scraped listing (owners and admins only see that tab)."""
    from catalog.models import MasterProduct

    rows = MeliListingProspect.objects.filter(organization=organization)
    shown = request.GET.get("status") if request.GET.get("status") in dict(SHOWN) else Status.PENDING
    query = request.GET.get("q", "").strip()
    if shown != "all":
        rows = rows.filter(status=shown)
    if query:
        rows = rows.filter(Q(title__icontains=query) | Q(external_id__icontains=query) | Q(seller__icontains=query))
    prospects = list(rows.select_related("product", "draft")[:PAGE + 1])
    more = len(prospects) > PAGE
    prospects = prospects[:PAGE]
    # Rows stored before categories were looked up, or whose job was lost, are queued again.
    request_categories(organization, prospects)
    for prospect in prospects:
        # stock, connected and started are read once for every row.
        prospect.reason = (
            _listable(organization, prospect.product, stock=stock, connected=connected, started=started)
            if prospect.product else ""
        )
    return {
        "prospects": prospects,
        "more": more,
        "shown": shown,
        "shown_choices": SHOWN,
        "query": query,
        "filtered": bool(query) or shown != Status.PENDING,
        "any_scraped": bool(prospects) or MeliListingProspect.objects.filter(organization=organization).exists(),
        "products": MasterProduct.objects.filter(organization=organization)
        .exclude(status=MasterProduct.Status.ARCHIVED)
        .order_by("sku")
        .values_list("sku", "title"),
        "scraped_organization": organization.pk,
        "back": request.get_full_path(),
        "bulk": deletion.bulk(
            "scraped",
            ("dismiss_selected", "Dismiss", "", False),
            ("restore_selected", "Back to pending", "", False),
            ("delete_selected", "Delete", "Delete {n} selected? A new scrape brings them back.", True),
            fields={"next": request.get_full_path()},
        ),
    }


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
    # The competitor's price is the suggestion; the product's list price when the scrape had none.
    draft = product_draft(
        organization, prospect.product, request.user, category_id=prospect.category_id, price=prospect.price
    )
    prospect.draft = draft
    prospect.status = Status.DRAFTED
    prospect.save(update_fields=["draft", "status", "updated_at"])
    return redirect(f"{reverse('meli_draft', args=[draft.pk])}?prospect={prospect.pk}")


@require_app(AppAccess.App.CATALOG)
@require_POST
def meli_draft_scraped_copy(request, pk):
    """Copy from a listing → the scraped listing: its category, then (once the fields load) its characteristics.

    Keeps what the user typed. A new category is checked again in the worker; the draft page waits for its
    fields and copies (?prospect=). Same category: the page copies at once.
    """
    from .draft_views import posted_attributes
    from .jobs import dispatch_drafts
    from .models import ListingDraft

    organization, can_edit = _scope(request)
    if not can_edit:
        return HttpResponseForbidden(READ_ONLY)
    draft = get_object_or_404(ListingDraft, organization=organization, pk=pk)
    if draft.locked() or not draft.regular():
        return HttpResponseForbidden("Posted drafts can't change.")
    prospect = get_object_or_404(MeliListingProspect, organization=organization, pk=request.POST.get("prospect"))
    draft.attributes = posted_attributes(request.POST)
    fields = ["attributes", "updated_at"]
    if prospect.category_id and prospect.category_id != draft.category_id and not draft.template:
        draft.category_id, draft.category_schema = prospect.category_id, []
        draft.status, draft.error, draft.error_details = ListingDraft.Status.CHECKING, "", {}
        fields += ["category_id", "category_schema", "status", "error", "error_details"]
    draft.save(update_fields=fields)
    if "status" in fields:
        dispatch_drafts(organization, "check", [draft.pk])
    response = HttpResponse(status=204)
    response["HX-Redirect"] = f"{reverse('meli_draft', args=[draft.pk])}?prospect={prospect.pk}"
    return response


@require_app(AppAccess.App.CATALOG)
@require_POST
def meli_draft_fast(request, pk):
    """Fast publishing: a draft straight from a scraped listing, with no Vimple product (link one later).

    Starts with the scraped title, photos (editable, another seller's), price and category; the page copies its
    characteristics once the category's fields load. Package and a quantity are typed on the draft.
    """
    from .description import default_parts
    from .jobs import dispatch_drafts
    from .models import FastListed, ListingDraft, MercadoLibreAccount

    organization, can_edit = _scope(request)
    if not can_edit:
        return HttpResponseForbidden(READ_ONLY)
    prospect = get_object_or_404(MeliListingProspect, organization=organization, pk=pk)
    if not MercadoLibreAccount.objects.filter(organization=organization).exists():
        messages.warning(request, "Connect Mercado Libre to list it.")
        return redirect(_back(request))
    draft = ListingDraft(
        organization=organization,
        kind=ListingDraft.Kind.FAST,
        title=prospect.title[:255],
        pictures=(prospect.gallery() or ([prospect.picture] if prospect.picture else []))[:10],
        price=prospect.price or 0,
        quantity=1,
        category_id=prospect.category_id,
        status=ListingDraft.Status.CHECKING,
        created_by=request.user,
    )
    # The copy blocks marked for new listings; there's no product description to start from.
    draft.description_parts = [part for part in default_parts(organization, FastListed(draft)) if not part.get("product")]
    draft.save()
    dispatch_drafts(organization, "check", [draft.pk])
    prospect.draft = draft
    prospect.status = Status.DRAFTED
    prospect.save(update_fields=["draft", "status", "updated_at"])
    return redirect(f"{reverse('meli_draft', args=[draft.pk])}?prospect={prospect.pk}")


UNDERCUT = Decimal("0.95")


@require_app(AppAccess.App.CATALOG)
def meli_product_competitors(request, pk):
    """The product page's suggested price: 5% below the lowest competitor (its linked scraped listings with a
    price, not dismissed), and every competitor's price in a dropdown. Loaded by htmx: catalog never imports meli."""
    from catalog.models import MasterProduct

    organization, can_edit = _scope(request)
    product = get_object_or_404(MasterProduct, organization=organization, pk=pk)
    competitors = list(
        MeliListingProspect.objects.filter(organization=organization, product=product, price__isnull=False)
        .exclude(status=Status.DISMISSED)
        .only("title", "external_id", "url", "seller", "price", "picture", "scraped_at", "updated_at")
        .order_by("price", "pk")[:20]
    )
    if not competitors:
        return HttpResponse("")
    lowest = competitors[0]
    suggested = (lowest.price * UNDERCUT).quantize(Decimal("1"), rounding=ROUND_FLOOR)
    return render(
        request,
        "meli/_competitor_price.html",
        {"competitors": competitors, "lowest": lowest, "suggested": suggested, "can_edit": can_edit},
    )
