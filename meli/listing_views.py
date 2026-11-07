from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from django.contrib import messages

from accounts.access import require_app
from vimple import deletion
from accounts.models import AppAccess, can_edit_products, membership_for

from .models import ListingTemplate, MercadoLibreAccount, MeliListing, MeliListingSync

PAGE_SIZE = 50
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
    rows = MeliListing.objects.filter(organization=organization).select_related("product")
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
    for listing in listings:
        listing.template = templates.get(listing.item_id)
    connected = MercadoLibreAccount.objects.filter(organization=organization).exists()
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
            ("pause_selected", "Pause", "", False),
            ("activate_selected", "Reactivate", "", False),
            ("close_selected", "Close", "Close {n} selected on Mercado Libre? Closing ends a listing for good: "
             "it can't be reopened. Its sales stay in Vimple.", True),
        ) if can_edit and connected else None,
        "active_tab": "meli",
    }
    template = "meli/_listings.html" if request.headers.get("HX-Request") == "true" else "meli/listings.html"
    return render(request, template, context)


BULK_STATUS = {"pause_selected": "paused", "activate_selected": "active", "close_selected": "closed"}
BULK_DONE = {"paused": "Pausing", "active": "Reactivating", "closed": "Closing"}


def _change_status(request, organization):
    """Pause, reactivate or close the selected listings on Mercado Libre, in the worker. Refusals show on each row."""
    from .jobs import dispatch_drafts_job

    status = BULK_STATUS[request.POST["action"]]
    rows = deletion.selected(request, MeliListing.objects.filter(organization=organization))
    item_ids = sorted({row.item_id for row in rows})
    if item_ids:
        MeliListing.objects.filter(organization=organization, item_id__in=item_ids).update(action_error="")
        dispatch_drafts_job(organization, "listing_status", item_ids=item_ids, status=status)
        count = len(item_ids)
        messages.success(
            request, f"{BULK_DONE[status]} {count} listing{'s' if count != 1 else ''} on Mercado Libre. "
            "The list shows the new status in a moment."
        )
    return redirect(request.get_full_path())


def _back(request):
    back = request.POST.get("next") or ""
    if not url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        back = reverse("meli_listings")
    return back
