from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Max, Q, Sum
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.dateparse import parse_date

from accounts.access import require_app
from accounts.models import AppAccess, can_edit_products, membership_for, user_can_open
from catalog.models import MasterProduct

from .forms import PurchaseForm, SupplierForm, product_label, read_lines
from .models import Purchase, PurchaseItem, Supplier

PAGE_SIZE = 50
READ_ONLY = "Only owners and admins can change purchases."
LINE_TOTAL = ExpressionWrapper(
    F("items__quantity") * F("items__unit_cost"),
    output_field=DecimalField(max_digits=20, decimal_places=2),
)


def _scope(request):
    membership = membership_for(request.user)
    return membership, membership.organization, can_edit_products(membership)


@require_app(AppAccess.App.PURCHASING)
def purchases(request):
    _membership, organization, can_edit = _scope(request)
    rows = (
        Purchase.objects.filter(organization=organization)
        .select_related("supplier")
        .annotate(line_count=Count("items"), total=Sum(LINE_TOTAL))
    )
    supplier = request.GET.get("supplier") or ""
    date_from = parse_date(request.GET.get("date_from") or "")
    date_to = parse_date(request.GET.get("date_to") or "")
    query = (request.GET.get("q") or "").strip()
    if supplier.isdigit():
        rows = rows.filter(supplier_id=supplier)
    if date_from:
        rows = rows.filter(purchased_on__gte=date_from)
    if date_to:
        rows = rows.filter(purchased_on__lte=date_to)
    if query:
        rows = rows.filter(Q(invoice_number__icontains=query) | Q(items__product__sku__icontains=query)).distinct()
    page = Paginator(rows.order_by("-purchased_on", "-created_at"), PAGE_SIZE).get_page(request.GET.get("page") or 1)
    context = {
        "purchases": page.object_list,
        "page_obj": page,
        "suppliers": Supplier.objects.filter(organization=organization),
        "supplier": supplier,
        "date_from": request.GET.get("date_from") or "",
        "date_to": request.GET.get("date_to") or "",
        "query": query,
        "can_edit": can_edit,
        "active_tab": "purchases",
    }
    template = "purchasing/_purchases.html" if request.headers.get("HX-Request") else "purchasing/purchases.html"
    return render(request, template, context)


@require_app(AppAccess.App.PURCHASING)
def purchase_edit(request, pk=None):
    membership, organization, can_edit = _scope(request)
    purchase = None
    if pk is not None:
        purchase = get_object_or_404(Purchase.objects.select_related("supplier"), organization=organization, pk=pk)
    if request.method == "POST" and not can_edit:
        return HttpResponseForbidden(READ_ONLY)
    can_link_receipts = user_can_open(request.user, AppAccess.App.CASHFLOW, membership)
    instance = purchase or Purchase(organization=organization)
    form = PurchaseForm(
        request.POST or None,
        instance=instance,
        organization=organization,
        can_link_receipts=can_link_receipts,
    )
    line_errors = []
    if purchase:
        lines = [
            {
                "id": str(item.pk),
                "product_text": product_label(item.product),
                "quantity_text": str(item.quantity),
                "unit_cost_text": str(item.unit_cost),
                "supplier_sku": item.supplier_sku,
                "errors": [],
            }
            for item in purchase.items.filter(organization=organization).select_related("product")
        ]
    else:
        lines = []
    if request.method == "POST":
        lines, line_errors = read_lines(request.POST, organization)
        if form.is_valid() and not line_errors:
            saved = _save(form, lines, organization)
            return redirect(f"{reverse('purchase', args=[saved.pk])}?saved=1")
    if not lines:
        lines = [{"id": "", "product_text": "", "quantity_text": "1", "unit_cost_text": "", "supplier_sku": "", "errors": []}]
    products = (
        MasterProduct.objects.filter(organization=organization)
        .exclude(status=MasterProduct.Status.ARCHIVED)
        .order_by("sku")
        .only("sku", "title")
    )
    return render(
        request,
        "purchasing/purchase.html",
        {
            "purchase": purchase,
            "form": form,
            "lines": lines,
            "line_errors": line_errors,
            "products": [product_label(product) for product in products],
            "saved": request.GET.get("saved") == "1",
            "can_edit": can_edit,
            "can_link_receipts": can_link_receipts,
            "active_tab": "purchases",
        },
    )


@transaction.atomic
def _save(form, lines, organization):
    """The purchase and exactly the submitted lines. Lines left out of the form are removed."""
    purchase = form.save()
    existing = {str(item.pk): item for item in purchase.items.filter(organization=organization)}
    kept = set()
    for line in lines:
        item = existing.get(line["id"]) or PurchaseItem(purchase=purchase, organization=organization)
        item.product = line["product"]
        item.quantity = line["quantity"]
        item.unit_cost = line["unit_cost"]
        item.supplier_sku = line["supplier_sku"]
        item.save()
        kept.add(str(item.pk))
    for key, item in existing.items():
        if key not in kept:
            item.delete()
    return purchase


@require_app(AppAccess.App.PURCHASING)
def suppliers(request):
    _membership, organization, can_edit = _scope(request)
    if request.method == "POST" and not can_edit:
        return HttpResponseForbidden(READ_ONLY)
    form = SupplierForm(request.POST or None, instance=Supplier(organization=organization))
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect("suppliers")
    rows = (
        Supplier.objects.filter(organization=organization)
        .annotate(purchase_count=Count("purchases"), last_purchase=Max("purchases__purchased_on"))
        .order_by("name")
    )
    return render(
        request,
        "purchasing/suppliers.html",
        {"suppliers": rows, "form": form, "can_edit": can_edit, "active_tab": "suppliers"},
    )


@require_app(AppAccess.App.PURCHASING)
def supplier_edit(request, pk):
    _membership, organization, can_edit = _scope(request)
    supplier = get_object_or_404(Supplier, organization=organization, pk=pk)
    if request.method == "POST" and not can_edit:
        return HttpResponseForbidden(READ_ONLY)
    form = SupplierForm(request.POST or None, instance=supplier)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect(f"{reverse('supplier', args=[supplier.pk])}?saved=1")
    history = (
        supplier.purchases.filter(organization=organization)
        .annotate(line_count=Count("items"), total=Sum(LINE_TOTAL))
        .order_by("-purchased_on", "-created_at")[:50]
    )
    return render(
        request,
        "purchasing/supplier.html",
        {
            "supplier": supplier,
            "form": form,
            "history": history,
            "saved": request.GET.get("saved") == "1",
            "can_edit": can_edit,
            "active_tab": "suppliers",
        },
    )
