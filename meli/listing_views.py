from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render

from accounts.access import require_app
from accounts.models import AppAccess, can_edit_products, membership_for

from .models import MercadoLibreAccount, MeliListing, MeliListingSync

PAGE_SIZE = 50
LINK_STATES = (("linked", "Linked"), ("unmatched", "SKU not in catalog"), ("no_sku", "No SKU"))
STATUSES = (
    ("active", "Active"),
    ("paused", "Paused"),
    ("closed", "Closed"),
    ("under_review", "Under review"),
    ("inactive", "Inactive"),
)


@require_app(AppAccess.App.CATALOG)
def meli_listings(request):
    membership = membership_for(request.user)
    organization = membership.organization
    can_edit = can_edit_products(membership)
    if request.method == "POST":
        if not can_edit:
            return HttpResponseForbidden("Only owners and admins can sync the listings.")
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
    if status in dict(STATUSES):
        rows = rows.filter(status=status)
    page = Paginator(rows.order_by("title", "item_id", "variation_id"), PAGE_SIZE).get_page(request.GET.get("page") or 1)
    context = {
        "listings": page.object_list,
        "page_obj": page,
        "record": record,
        "connected": MercadoLibreAccount.objects.filter(organization=organization).exists(),
        "query": query,
        "link": link,
        "status": status,
        "link_states": LINK_STATES,
        "statuses": STATUSES,
        "can_edit": can_edit,
        "active_tab": "meli",
    }
    template = "meli/_listings.html" if request.headers.get("HX-Request") == "true" else "meli/listings.html"
    return render(request, template, context)
