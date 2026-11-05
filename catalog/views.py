import json

from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from accounts.access import require_app
from accounts.models import AppAccess, can_edit_products, membership_for, user_can_open

from .forms import (
    BrandForm,
    CategoryForm,
    ProductForm,
    ProductModelForm,
    VariantForm,
    attribute_rows,
    attributes_from,
)
from .models import Brand, Category, MasterProduct, ProductModel
from .validators import validate_attributes

PAGE_SIZE = 50
READ_ONLY = "Only owners and admins can change the catalog."
# Categories and brands: the model, its form, and the Codes page's examples (name, code).
_KINDS = {
    "category": (Category, CategoryForm, "Perfumes", "PERF"),
    "brand": (Brand, BrandForm, "Only", "ONLY"),
}


def _scope(request):
    """The active organization and whether this user may edit. Every query filters on it."""
    membership = membership_for(request.user)
    return membership.organization, can_edit_products(membership)


@require_app(AppAccess.App.CATALOG)
def products(request):
    organization, can_edit = _scope(request)
    rows = MasterProduct.objects.filter(organization=organization).select_related(
        "product_model__brand", "product_model__category"
    )
    query = (request.GET.get("q") or "").strip()
    brand = request.GET.get("brand") or ""
    category = request.GET.get("category") or ""
    status = request.GET.get("status") or ""
    if query:
        rows = rows.filter(Q(sku__icontains=query) | Q(title__icontains=query) | Q(gtin__icontains=query))
    if brand.isdigit():
        rows = rows.filter(product_model__brand_id=brand)
    if category.isdigit():
        rows = rows.filter(product_model__category_id=category)
    if status in MasterProduct.Status.values:
        rows = rows.filter(status=status)
    page = Paginator(rows.order_by("sku"), PAGE_SIZE).get_page(request.GET.get("page") or 1)
    context = {
        "products": page.object_list,
        "page_obj": page,
        "query": query,
        "brand": brand,
        "category": category,
        "status": status,
        "brands": Brand.objects.filter(organization=organization),
        "categories": Category.objects.filter(organization=organization),
        "statuses": MasterProduct.Status.choices,
        "can_edit": can_edit,
        "active_tab": "products",
    }
    template = "catalog/_products.html" if request.headers.get("HX-Request") else "catalog/products.html"
    return render(request, template, context)


@require_app(AppAccess.App.CATALOG)
def models(request):
    organization, can_edit = _scope(request)
    rows = (
        ProductModel.objects.filter(organization=organization)
        .select_related("brand", "category")
        .annotate(variant_count=Count("products"))
        .order_by("brand__code", "code")
    )
    return render(
        request,
        "catalog/models.html",
        {"models": rows, "can_edit": can_edit, "active_tab": "models"},
    )


@require_app(AppAccess.App.CATALOG)
def model_edit(request, pk=None):
    organization, can_edit = _scope(request)
    model = None
    if pk is not None:
        model = get_object_or_404(ProductModel, organization=organization, pk=pk)
    if request.method == "POST" and not can_edit:
        return HttpResponseForbidden(READ_ONLY)
    instance = model or ProductModel(organization=organization)
    form = ProductModelForm(organization=organization, instance=instance)
    variant_form = VariantForm(prefix="variant", instance=_new_variant(organization, model))
    if request.method == "POST" and request.POST.get("action") == "variant" and model:
        variant_form = VariantForm(request.POST, prefix="variant", instance=_new_variant(organization, model))
        if variant_form.is_valid():
            variant_form.save()
            return redirect(f"{reverse('catalog_model', args=[model.pk])}?added={variant_form.instance.pk}")
    elif request.method == "POST":
        form = ProductModelForm(request.POST, organization=organization, instance=instance)
        if form.is_valid():
            saved = form.save()
            return redirect("catalog_model", pk=saved.pk)
    variants = []
    if model:
        variants = model.products.filter(organization=organization).order_by("sku")
    return render(
        request,
        "catalog/model.html",
        {
            "model": model,
            "form": form,
            "variant_form": variant_form,
            "variants": variants,
            "added": request.GET.get("added") or "",
            "can_edit": can_edit,
            "active_tab": "models",
            "codes": _codes(organization),
            "code_dialogs": [_code_dialog(organization, kind) for kind in _KINDS] if can_edit else [],
        },
    )


@require_app(AppAccess.App.CATALOG)
@require_POST
def code_new(request, kind):
    """Add a category or brand from the model page's dialog, without leaving it.

    Errors come back as the dialog's form; a saved row comes back as a 204 with an
    HX-Trigger event that adds it to the select.
    """
    if kind not in _KINDS:
        raise Http404
    organization, can_edit = _scope(request)
    if not can_edit:
        return HttpResponseForbidden(READ_ONLY)
    dialog = _code_dialog(organization, kind, request.POST)
    form = dialog["form"]
    if not form.is_valid():
        return render(request, "catalog/_code_dialog_form.html", {"dialog": dialog})
    row = form.save()
    # 204: htmx leaves the dialog's form in place and still fires the event.
    response = HttpResponse(status=204)
    response["HX-Trigger"] = json.dumps(
        {
            "select-option-added": {
                "select": f"id_{kind}",
                "dialog": f"code-dialog-{kind}",
                "id": str(row.pk),
                "label": str(row),
                "kind": kind,
                "code": row.code,
            }
        }
    )
    return response


def _code_dialog(organization, kind, data=None):
    Model, Form, example_name, example_code = _KINDS[kind]
    return {
        "kind": kind,
        "label": Model._meta.verbose_name,
        "form": Form(data, prefix=kind, instance=Model(organization=organization)),
        "example_name": example_name,
        "example_code": example_code,
    }


def _new_variant(organization, model):
    return MasterProduct(organization=organization, product_model=model)


def _codes(organization):
    """Category and brand codes by id, for the live SKU preview."""
    return {
        "categories": {str(row.pk): row.code for row in Category.objects.filter(organization=organization)},
        "brands": {str(row.pk): row.code for row in Brand.objects.filter(organization=organization)},
    }


@require_app(AppAccess.App.CATALOG)
def product_detail(request, pk):
    organization, can_edit = _scope(request)
    product = get_object_or_404(
        MasterProduct.objects.select_related("product_model__brand", "product_model__category"),
        organization=organization,
        pk=pk,
    )
    if request.method == "POST" and not can_edit:
        return HttpResponseForbidden(READ_ONLY)
    form = ProductForm(instance=product)
    attributes = attribute_rows(product.attributes)
    attributes_error = ""
    if request.method == "POST":
        form = ProductForm(request.POST, instance=product)
        submitted = attributes_from(request.POST)
        attributes = attribute_rows(submitted)
        try:
            validate_attributes(submitted)
        except ValidationError as error:
            attributes_error = " ".join(error.messages)
        if form.is_valid() and not attributes_error:
            saved = form.save(commit=False)
            saved.attributes = submitted
            saved.save()
            return redirect(f"{reverse('catalog_product', args=[product.pk])}?saved=1")
    listings = product.meli_listings.filter(organization=organization).order_by("status", "item_id")
    can_see_costs = user_can_open(request.user, AppAccess.App.PURCHASING)
    costs, lines = [], []
    if can_see_costs:
        costs = product.costs_by_supplier()
        lines = (
            product.purchase_items.filter(organization=organization)
            .select_related("purchase__supplier")
            .order_by("-purchase__purchased_on", "-pk")[:10]
        )
    return render(
        request,
        "catalog/product.html",
        {
            "product": product,
            "form": form,
            "attributes": attributes,
            "attributes_error": attributes_error,
            "variants": product.variants().order_by("sku"),
            "listings": listings,
            "can_see_costs": can_see_costs,
            "costs": costs,
            "lines": lines,
            "saved": request.GET.get("saved") == "1",
            "can_edit": can_edit,
            "active_tab": "products",
        },
    )


@require_app(AppAccess.App.CATALOG)
def codes(request):
    """Categories and brands: add one, or change a name or an unused code."""
    organization, can_edit = _scope(request)
    if request.method == "POST" and not can_edit:
        return HttpResponseForbidden(READ_ONLY)
    forms = {kind: Form(prefix=kind, instance=Model(organization=organization)) for kind, (Model, Form, *_) in _KINDS.items()}
    editing = {}
    if request.method == "POST" and request.POST.get("kind") in _KINDS:
        kind = request.POST["kind"]
        Model, Form, *_ = _KINDS[kind]
        row = Model(organization=organization)
        if request.POST.get("id"):
            row = get_object_or_404(Model, organization=organization, pk=request.POST["id"])
        form = Form(request.POST, prefix=kind, instance=row)
        if form.is_valid():
            form.save()
            return redirect("catalog_codes")
        if row.pk:
            editing[kind] = (row.pk, form)
        else:
            forms[kind] = form
    categories = Category.objects.filter(organization=organization).annotate(model_count=Count("product_models"))
    brands = Brand.objects.filter(organization=organization).annotate(model_count=Count("product_models"))
    return render(
        request,
        "catalog/codes.html",
        {
            "sections": [
                ("category", "Categories", "Perfumes", "PERF", categories, forms["category"], editing.get("category")),
                ("brand", "Brands", "Only", "ONLY", brands, forms["brand"], editing.get("brand")),
            ],
            "can_edit": can_edit,
            "active_tab": "codes",
        },
    )
