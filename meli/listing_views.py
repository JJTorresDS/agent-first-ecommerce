from datetime import timedelta

from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from urllib.parse import urlparse

from django.contrib import messages

from accounts.access import require_app
from vimple import deletion
from accounts.models import AppAccess, can_edit_products, membership_for

from .models import ListingChange, ListingTemplate, MercadoLibreAccount, MeliListing, MeliListingSync

PAGE_SIZE = 50
PENDING_POLL = timedelta(minutes=5)
LINK_STATES = (
    ("linked", "Linked"),
    ("unmatched", "SKU not in catalog"),
    ("no_sku", "No SKU"),
    ("template", "Templates"),
)
STATUSES = (
    ("active", "Active"),
    ("paused", "Paused"),
    ("closed", "Closed"),
    ("under_review", "Under review"),
    ("inactive", "Inactive"),
)


# The order a product's listings are summed up in; closed ones only when nothing else is left.
SUMMARY_ORDER = ("active", "paused", "under_review", "inactive")


def listing_counts(organization, products=(), combos=()):
    """Mercado Libre items per status for these products and combos, in one query.

    Items, not rows: a listing with three variations is one item.
    """
    product_ids = [row.pk for row in products]
    combo_ids = [row.pk for row in combos]
    if not product_ids and not combo_ids:
        return {}
    counts = {}
    rows = (
        MeliListing.objects.filter(organization=organization)
        .filter(Q(product_id__in=product_ids) | Q(combo_id__in=combo_ids))
        .values("product_id", "combo_id", "status")
        .annotate(items=Count("item_id", distinct=True))
    )
    for row in rows:
        key = ("product", row["product_id"]) if row["product_id"] else ("combo", row["combo_id"])
        counts.setdefault(key, {})[row["status"] or "unknown"] = row["items"]
    return counts


def listing_summary(counts):
    """ "2 active, 1 paused"; "1 closed" when that's all; "" when it was never listed."""
    labels = dict(STATUSES)
    parts = [f"{counts[status]} {labels[status].lower()}" for status in SUMMARY_ORDER if counts.get(status)]
    if not parts and counts:
        parts = [f"{number} {labels.get(status, status).lower()}" for status, number in counts.items()]
    return ", ".join(parts)


@require_app(AppAccess.App.CATALOG)
def meli_listings(request):
    membership = membership_for(request.user)
    organization = membership.organization
    can_edit = can_edit_products(membership)
    if request.method == "POST":
        if not can_edit:
            return HttpResponseForbidden("Only owners and admins can sync the listings.")
        if request.POST.get("action") in ("template", "untemplate"):
            from .listing_templates import mark

            listing = get_object_or_404(MeliListing, organization=organization, pk=request.POST.get("listing"))
            if request.POST["action"] == "untemplate":
                # Only the mark goes: the listing stays paused; Vimple never activates a template.
                ListingTemplate.objects.filter(organization=organization, item_id=listing.item_id).delete()
            elif listing.category_id:
                mark(organization, listing)
            return redirect(_back(request))
        if request.POST.get("action") in BULK_STATUS:
            return _change_status(request, organization)
        if request.POST.get("action") == "edit":
            return _edit(request, organization)
        if request.POST.get("action") == "units":
            # Units one sale takes: 2 for a Kit x2. Past sales keep what they took.
            listing = get_object_or_404(MeliListing, organization=organization, pk=request.POST.get("listing"))
            try:
                units = int(request.POST.get("units_per_sale") or "")
            except ValueError:
                units = 0
            if 1 <= units <= 100:
                listing.units_per_sale = units
                listing.save(update_fields=["units_per_sale"])
            return redirect(_back(request))
        from .jobs import dispatch_listing_sync

        if MercadoLibreAccount.objects.filter(organization=organization).exists():
            dispatch_listing_sync(organization)
        return redirect("meli_listings")
    record = MeliListingSync.objects.filter(organization=organization).first()
    if request.GET.get("poll") == "sync" and request.headers.get("HX-Request") == "true":
        response = render(request, "meli/_listing_sync.html", {"record": record, "can_edit": can_edit})
        if record is None or not record.busy():
            # The sync ended: reload so the table shows what it found.
            response["HX-Refresh"] = "true"
        return response
    rows = (
        MeliListing.objects.filter(organization=organization)
        .select_related("product", "combo")
        .prefetch_related("combo__items")
    )
    query = (request.GET.get("q") or "").strip()
    link = request.GET.get("link") or ""
    status = request.GET.get("status") or ""
    if query:
        rows = rows.filter(Q(item_id__icontains=query) | Q(title__icontains=query) | Q(seller_sku__icontains=query))
    if link == "linked":
        rows = rows.filter(product__isnull=False)
    elif link == "unmatched":
        rows = rows.filter(product__isnull=True).exclude(seller_sku="")
    elif link == "no_sku":
        rows = rows.filter(product__isnull=True, seller_sku="")
    elif link == "template":
        rows = rows.filter(
            item_id__in=ListingTemplate.objects.filter(organization=organization).values("item_id")
        )
    if status in dict(STATUSES):
        rows = rows.filter(status=status)
    else:
        # Closed listings are over: they show only when asked for.
        rows = rows.exclude(status="closed")
    page = Paginator(rows.order_by("title", "item_id", "variation_id"), PAGE_SIZE).get_page(request.GET.get("page") or 1)
    listings = list(page.object_list)
    templates = {
        row.item_id: row
        for row in ListingTemplate.objects.filter(
            organization=organization, item_id__in={listing.item_id for listing in listings}
        )
    }
    connected = MercadoLibreAccount.objects.filter(organization=organization).exists()
    _decorate(organization, listings, templates, editing=can_edit and connected)
    context = {
        "listings": listings,
        "page_obj": page,
        "record": record,
        "connected": connected,
        "query": query,
        "link": link,
        "status": status,
        "link_states": LINK_STATES,
        "statuses": STATUSES,
        "can_edit": can_edit,
        "bulk": deletion.bulk(
            "listings",
            ("pause_selected", "Pause", "", False, "status-dialog"),
            ("activate_selected", "Reactivate", "", False, "status-dialog"),
            ("close_selected", "Close", "", True, "status-dialog"),
        ) if can_edit and connected else None,
        "reasons": ListingChange.Reason.choices,
        "back": request.get_full_path(),
        "active_tab": "meli",
    }
    template = "meli/_listings.html" if request.headers.get("HX-Request") == "true" else "meli/listings.html"
    return render(request, template, context)


BULK_STATUS = {"pause_selected": "paused", "activate_selected": "active", "close_selected": "closed"}
BULK_DONE = {"paused": "Pausing", "active": "Reactivating", "closed": "Closing"}


def _change_status(request, organization):
    """Pause, reactivate or close the selected listings on Mercado Libre, in the worker. Refusals show on each row.

    Each needs a reason (Other needs a note); one ListingChange per item keeps who, why and how it went.
    """
    from .jobs import dispatch_drafts_job

    status = BULK_STATUS[request.POST["action"]]
    reason = request.POST.get("reason") or ""
    note = (request.POST.get("note") or "").strip()[:500]
    if reason not in ListingChange.Reason.values:
        messages.error(request, "Pick a reason first: nothing was changed.")
        return redirect(request.get_full_path())
    if reason == ListingChange.Reason.OTHER and not note:
        messages.error(request, "Say why in the note when the reason is Other: nothing was changed.")
        return redirect(request.get_full_path())
    rows = deletion.selected(request, MeliListing.objects.filter(organization=organization))
    first = {}
    for row in rows:
        first.setdefault(row.item_id, row)
    item_ids = sorted(first)
    if item_ids:
        changes = ListingChange.objects.bulk_create([
            ListingChange(
                organization=organization,
                item_id=item_id,
                title=first[item_id].title,
                field=ListingChange.Field.STATUS,
                old_value=first[item_id].status,
                new_value=status,
                reason=reason,
                note=note,
                user=request.user,
            )
            for item_id in item_ids
        ])
        MeliListing.objects.filter(organization=organization, item_id__in=item_ids).update(action_error="")
        dispatch_drafts_job(
            organization, "listing_status", item_ids=item_ids, status=status,
            change_ids=[change.pk for change in changes],
        )
        count = len(item_ids)
        messages.success(
            request, f"{BULK_DONE[status]} {count} listing{'s' if count != 1 else ''} on Mercado Libre. "
            "The list shows the new status in a moment."
        )
    return redirect(request.get_full_path())


def latest_changes(organization, item_ids):
    """Each item's newest ListingChange, in one query."""
    latest = {}
    if not item_ids:
        return latest
    rows = (
        ListingChange.objects.filter(organization=organization, item_id__in=list(item_ids))
        .select_related("user")
        .order_by("item_id", "-created_at", "-pk")
    )
    for row in rows:
        latest.setdefault(row.item_id, row)
    return latest


def _waiting(change):
    """The worker usually answers in seconds: a row polls while its change is queued, for a few minutes at most."""
    return (
        change is not None
        and change.state == ListingChange.State.QUEUED
        and change.created_at >= timezone.now() - PENDING_POLL
    )


def _decorate(organization, listings, templates, editing):
    """What each row shows besides its own fields: template, last change, polling, stock cap (editors only)."""
    from .listing_edits import stock_limits

    last_changes = latest_changes(organization, {listing.item_id for listing in listings})
    limits = stock_limits(organization, listings) if editing else {}
    for listing in listings:
        listing.template = templates.get(listing.item_id)
        listing.last_change = last_changes.get(listing.item_id)
        listing.polling = _waiting(listing.last_change)
        listing.stock_limit = limits.get(listing.pk)


@require_app(AppAccess.App.CATALOG)
def meli_listing_row(request, pk):
    """One listing's row, polled while its change is queued: 204 (no swap) until the worker is done."""
    membership = membership_for(request.user)
    organization = membership.organization
    listing = get_object_or_404(
        MeliListing.objects.select_related("product", "combo").prefetch_related("combo__items"),
        organization=organization,
        pk=pk,
    )
    change = latest_changes(organization, {listing.item_id}).get(listing.item_id)
    if _waiting(change):
        return HttpResponse(status=204)
    can_edit = can_edit_products(membership)
    connected = MercadoLibreAccount.objects.filter(organization=organization).exists()
    templates = {
        row.item_id: row for row in ListingTemplate.objects.filter(organization=organization, item_id=listing.item_id)
    }
    _decorate(organization, [listing], templates, editing=can_edit and connected)
    current = request.headers.get("HX-Current-URL") or ""
    back = urlparse(current).path if url_has_allowed_host_and_scheme(current, allowed_hosts={request.get_host()}) else ""
    if back and urlparse(current).query:
        back += "?" + urlparse(current).query
    return render(request, "meli/_listing_row.html", {
        "row": listing,
        "can_edit": can_edit,
        "bulk": {"id": "bulk-listings"} if can_edit and connected else None,
        "back": back or reverse("meli_listings"),
    })


def _edit(request, organization):
    """Change one listing's price or available quantity on Mercado Libre, with a reason, in the worker."""
    from .jobs import dispatch_drafts_job
    from .listing_edits import EDIT_FIELDS, edit_problem, read_price, read_quantity, stock_limits

    listing = get_object_or_404(
        MeliListing.objects.select_related("combo").prefetch_related("combo__items"),
        organization=organization,
        pk=request.POST.get("listing"),
    )
    back = _back(request)
    field = request.POST.get("field") or ""
    reason = request.POST.get("reason") or ""
    note = (request.POST.get("note") or "").strip()[:500]
    if field not in EDIT_FIELDS:
        return redirect(back)
    price = field == ListingChange.Field.PRICE
    value = read_price(request.POST.get("value")) if price else read_quantity(request.POST.get("value"))
    current = listing.price if price else listing.available_quantity
    limit = None if price else stock_limits(organization, [listing]).get(listing.pk)
    if value is None:
        problem = "Type a price above zero." if price else "Type a whole number of units, 0 or more."
    elif value == current:
        problem = f"That's already its {'price' if price else 'available quantity'}."
    elif reason not in ListingChange.Reason.values:
        problem = "Pick a reason first"
    elif reason == ListingChange.Reason.OTHER and not note:
        problem = "Say why in the note when the reason is Other"
    else:
        problem = edit_problem(listing, field, value, limit)
    if problem:
        messages.error(request, f"{listing.item_id}: {problem.rstrip('.')}. Nothing was changed.")
        return redirect(back)
    change = ListingChange.objects.create(
        organization=organization,
        item_id=listing.item_id,
        variation_id="" if price else listing.variation_id,
        title=listing.title,
        field=field,
        old_value="" if current is None else str(current),
        new_value=str(value),
        reason=reason,
        note=note,
        user=request.user,
    )
    MeliListing.objects.filter(pk=listing.pk).update(action_error="")
    dispatch_drafts_job(organization, "listing_edit", change_id=change.pk)
    messages.success(request, f"{change.pending_verb()} for {listing.item_id} on Mercado Libre.")
    return redirect(back)


@require_app(AppAccess.App.CATALOG)
def meli_listing_history(request, item_id):
    """This organization's changes to one listing from Vimple, newest first, for the History dialog."""
    organization = membership_for(request.user).organization
    changes = (
        ListingChange.objects.filter(organization=organization, item_id=item_id)
        .select_related("user")
        .order_by("-created_at", "-pk")[:100]
    )
    return render(request, "meli/_listing_history.html", {"changes": changes, "item_id": item_id})


def _back(request):
    back = request.POST.get("next") or ""
    if not url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        back = reverse("meli_listings")
    return back
