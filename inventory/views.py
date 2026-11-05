from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.dateparse import parse_date

from accounts.access import require_app
from accounts.models import AppAccess, can_edit_products, membership_for
from catalog.models import MasterProduct

from . import ledger
from .models import StockLocation, StockMovement

PAGE_SIZE = 50
READ_ONLY = "Only owners and admins can change stock."
OWN, FULL = ledger.OWN, ledger.FULL


def _scope(request):
    membership = membership_for(request.user)
    return membership.organization, can_edit_products(membership), request.user.email or request.user.get_username()


def _stock_rows(organization):
    """Each product with Vimple's stock, its linked listings and whether Mercado Libre disagrees."""
    from meli.models import MeliListing

    stock = ledger.on_hand_by_product(organization)
    listings = {}
    for listing in MeliListing.objects.filter(organization=organization, product__isnull=False).exclude(status="closed"):
        listings.setdefault(listing.product_id, []).append(listing)
    rows = []
    products = MasterProduct.objects.filter(organization=organization).select_related("product_model").order_by("sku")
    for product in products:
        units = stock.get(product.pk, {OWN: 0, FULL: 0})
        linked = listings.get(product.pk, [])
        if product.status == MasterProduct.Status.ARCHIVED and not any(units.values()) and not linked:
            continue
        own_listings = [row for row in linked if not row.is_full()]
        full_listings = [row for row in linked if row.is_full()]
        # A listing shows whole sales: a Kit x2 can sell half the units.
        own_off = [
            row for row in own_listings
            if row.available_quantity is not None and row.available_quantity != max(0, units[OWN]) // max(1, row.units_per_sale)
        ]
        full_known = {row.inventory_id: row.full_available for row in full_listings if row.full_available is not None}
        meli_full = sum(full_known.values()) if full_known else None
        rows.append(
            {
                "product": product,
                "own": units[OWN],
                "full": units[FULL],
                "total": units[OWN] + units[FULL],
                "listings": own_listings,
                "meli_full": meli_full,
                "own_off": bool(own_off),
                "full_off": meli_full is not None and meli_full != units[FULL],
            }
        )
    return rows


@require_app(AppAccess.App.CATALOG)
def stock(request):
    organization, can_edit, _by = _scope(request)
    rows = _stock_rows(organization)
    query = (request.GET.get("q") or "").strip().upper()
    show = request.GET.get("show") or ""
    if query:
        rows = [row for row in rows if query in row["product"].sku or query in row["product"].title.upper()]
    if show == "differences":
        rows = [row for row in rows if row["own_off"] or row["full_off"]]
    elif show == "out":
        rows = [row for row in rows if row["total"] <= 0]
    settings = ledger.settings_for(organization)
    context = {
        "rows": rows,
        "query": request.GET.get("q") or "",
        "show": show,
        "started_at": settings.started_at,
        "can_edit": can_edit,
        "active_tab": "stock",
    }
    template = "inventory/_stock.html" if request.headers.get("HX-Request") == "true" else "inventory/stock.html"
    return render(request, template, context)


@require_app(AppAccess.App.CATALOG)
def count(request):
    """A stock take. The first save starts inventory: Mercado Libre sales come off from then on."""
    organization, can_edit, by = _scope(request)
    if request.method == "POST" and not can_edit:
        return HttpResponseForbidden(READ_ONLY)
    products = list(
        MasterProduct.objects.filter(organization=organization)
        .exclude(status=MasterProduct.Status.ARCHIVED)
        .order_by("sku")
    )
    stock = ledger.on_hand_by_product(organization)
    errors = []
    if request.method == "POST":
        counted = []
        for product in products:
            for where in (OWN, FULL):
                raw = (request.POST.get(f"{where}_{product.pk}") or "").strip()
                if raw == "":
                    continue
                try:
                    units = int(raw)
                    if units < 0:
                        raise ValueError
                except ValueError:
                    errors.append(f"{product.sku}: enter a whole number of 0 or more.")
                    continue
                if units != stock.get(product.pk, {OWN: 0, FULL: 0})[where]:
                    counted.append((product, where, units))
        if not errors:
            opening = ledger.start(organization, by=by)
            for product, where, units in counted:
                ledger.count(organization, product, where, units, opening=opening, by=by)
            if opening:
                ledger.apply_pending_sales(organization)
            return redirect(f"{reverse('inventory_stock')}?counted={len(counted)}")
    rows = [(product, stock.get(product.pk, {OWN: 0, FULL: 0})) for product in products]
    return render(
        request,
        "inventory/count.html",
        {
            "rows": rows,
            "errors": errors,
            "started_at": ledger.settings_for(organization).started_at,
            "can_edit": can_edit,
            "active_tab": "count",
        },
    )


@require_app(AppAccess.App.CATALOG)
def movements(request):
    organization, can_edit, _by = _scope(request)
    rows = StockMovement.objects.filter(organization=organization).select_related(
        "product", "location", "purchase_item__purchase", "meli_sale"
    )
    query = (request.GET.get("q") or "").strip()
    kind = request.GET.get("kind") or ""
    where = request.GET.get("location") or ""
    date_from = parse_date(request.GET.get("date_from") or "")
    date_to = parse_date(request.GET.get("date_to") or "")
    if query:
        rows = rows.filter(Q(product__sku__icontains=query) | Q(product__title__icontains=query) | Q(note__icontains=query))
    if kind in StockMovement.Kind.values:
        rows = rows.filter(kind=kind)
    if where in StockLocation.Kind.values:
        rows = rows.filter(location__kind=where)
    if date_from:
        rows = rows.filter(occurred_at__date__gte=date_from)
    if date_to:
        rows = rows.filter(occurred_at__date__lte=date_to)
    page = Paginator(rows, PAGE_SIZE).get_page(request.GET.get("page") or 1)
    context = {
        "movements": page.object_list,
        "page_obj": page,
        "query": query,
        "kind": kind,
        "location": where,
        "date_from": request.GET.get("date_from") or "",
        "date_to": request.GET.get("date_to") or "",
        "kinds": StockMovement.Kind.choices,
        "locations": StockLocation.Kind.choices,
        "can_edit": can_edit,
        "active_tab": "movements",
    }
    template = "inventory/_movements.html" if request.headers.get("HX-Request") == "true" else "inventory/movements.html"
    return render(request, template, context)


def sale_state(sale, started_at):
    if sale.applied:
        return ("put_back", "Cancelled, put back") if sale.cancelled() else ("taken", "Taken from stock")
    if sale.listing_id is None or sale.listing is None or sale.listing.product_id is None:
        return "unlinked", "No catalog product"
    if started_at is None or sale.sold_at < started_at:
        return "before", "Before the first count"
    if sale.cancelled():
        return "cancelled", "Cancelled"
    return "waiting", "Not paid yet"


@require_app(AppAccess.App.CATALOG)
def sales(request):
    from meli.models import MercadoLibreAccount, MeliSale, MeliSalesSync

    organization, can_edit, _by = _scope(request)
    if request.method == "POST":
        if not can_edit:
            return HttpResponseForbidden(READ_ONLY)
        from meli.jobs import dispatch_sales_sync

        dispatch_sales_sync(organization)
        return redirect("inventory_sales")
    started_at = ledger.settings_for(organization).started_at
    rows = MeliSale.objects.filter(organization=organization).select_related("listing__product")
    state = request.GET.get("state") or ""
    if state == "unlinked":
        rows = rows.filter(applied=False).filter(Q(listing__isnull=True) | Q(listing__product__isnull=True))
    elif state == "taken":
        rows = rows.filter(applied=True)
    page = Paginator(rows, PAGE_SIZE).get_page(request.GET.get("page") or 1)
    for sale in page.object_list:
        sale.state, sale.state_label = sale_state(sale, started_at)
    return render(
        request,
        "inventory/sales.html",
        {
            "sales": page.object_list,
            "page_obj": page,
            "state": state,
            "record": MeliSalesSync.objects.filter(organization=organization).first(),
            "connected": MercadoLibreAccount.objects.filter(organization=organization).exists(),
            "started_at": started_at,
            "can_edit": can_edit,
            "active_tab": "sales",
        },
    )


@require_app(AppAccess.App.CATALOG)
def product_stock(request, pk):
    """The Stock card on a product page, and its Adjust, Count and Send to Full forms."""
    organization, can_edit, by = _scope(request)
    product = get_object_or_404(MasterProduct, organization=organization, pk=pk)
    error = ""
    if request.method == "POST":
        if not can_edit:
            return HttpResponseForbidden(READ_ONLY)
        action = request.POST.get("action")
        try:
            units = int(request.POST.get("units") or "")
        except ValueError:
            units = None
        note = (request.POST.get("note") or "").strip()
        try:
            if units is None:
                raise ValidationError("Enter a whole number.")
            if action == "adjust":
                ledger.adjust(organization, product, request.POST.get("kind") or "", units, by=by, note=note)
            elif action == "count":
                where = request.POST.get("location") if request.POST.get("location") in (OWN, FULL) else OWN
                opening = ledger.start(organization, by=by)
                ledger.count(organization, product, where, units, opening=opening, by=by)
                if opening:
                    ledger.apply_pending_sales(organization)
            elif action == "full":
                ledger.send_to_full(organization, product, units, by=by, note=note)
            else:
                raise ValidationError("Choose what to do.")
        except ValidationError as failure:
            error = " ".join(failure.messages)
    units = ledger.on_hand(product)
    recent = product.stock_movements.filter(organization=organization).select_related("location")[:10]
    return render(
        request,
        "inventory/_product_stock.html",
        {
            "product": product,
            "own": units[OWN],
            "full": units[FULL],
            "recent": recent,
            "error": error,
            "saved": request.method == "POST" and not error,
            "manual_kinds": [(kind, StockMovement.Kind(kind).label) for kind in ledger.MANUAL_KINDS],
            "can_edit": can_edit,
        },
    )
