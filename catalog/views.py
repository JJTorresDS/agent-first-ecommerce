import json

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from accounts.access import require_app
from accounts.models import AppAccess, can_edit_products, membership_for, user_can_open

from .forms import (
    CopyBlockForm,
    ComboForm,
    BrandForm,
    CategoryForm,
    ProductForm,
    ProductModelForm,
    VariantForm,
    attribute_rows,
    attributes_from,
    read_combo_lines,
)
from vimple import deletion
from vimple.ai import enabled as ai_enabled

from .deletion import code_blocked, combo_blocked, model_blocked, product_blocked, unlinked
from .images import set_images
from .models import Brand, Category, Combo, ComboItem, CopyBlock, MasterProduct, ProductModel, package_missing
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
    if request.method == "POST":
        if not can_edit:
            return HttpResponseForbidden(READ_ONLY)
        everything = MasterProduct.objects.filter(organization=organization)
        if request.POST.get("action") == "archive_selected":
            return deletion.archive_selected(request, everything, request.get_full_path())
        if request.POST.get("action") == "delete_selected":
            return deletion.delete_selected(request, everything, product_blocked, lambda row: row.sku, request.get_full_path())
        return redirect(request.get_full_path())
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
        "bulk": deletion.bulk("products", deletion.ARCHIVE_ACTION, deletion.DELETE_ACTION) if can_edit else None,
        "active_tab": "products",
    }
    template = "catalog/_products.html" if request.headers.get("HX-Request") else "catalog/products.html"
    return render(request, template, context)


@require_app(AppAccess.App.CATALOG)
def models(request):
    organization, can_edit = _scope(request)
    if request.method == "POST":
        if not can_edit:
            return HttpResponseForbidden(READ_ONLY)
        if request.POST.get("action") == "delete_selected":
            return deletion.delete_selected(
                request, ProductModel.objects.filter(organization=organization), model_blocked, lambda row: row.name,
                "catalog_models",
            )
        return redirect("catalog_models")
    rows = (
        ProductModel.objects.filter(organization=organization)
        .select_related("brand", "category")
        .annotate(variant_count=Count("products"))
        .order_by("brand__code", "code")
    )
    return render(
        request,
        "catalog/models.html",
        {"models": rows, "can_edit": can_edit, "active_tab": "models",
         "bulk": deletion.bulk("models", deletion.DELETE_ACTION) if can_edit else None},
    )


@require_app(AppAccess.App.CATALOG)
def model_edit(request, pk=None):
    organization, can_edit = _scope(request)
    model = None
    if pk is not None:
        model = get_object_or_404(ProductModel, organization=organization, pk=pk)
    if request.method == "POST" and not can_edit:
        return HttpResponseForbidden(READ_ONLY)
    if request.method == "POST" and request.POST.get("action") == "delete" and model:
        return deletion.delete(request, model, model_blocked(model), request.path, "catalog_models", model.name)
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
            "delete": deletion.card("This model", model_blocked(model)) if can_edit and model else None,
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


AI_FAILED = "The suggestion didn't come through; fill it in yourself."


@require_app(AppAccess.App.CATALOG)
@require_POST
def ai_suggest(request, kind):
    """AI codes or description for the form that asked, sent as an ai-suggested event. Nothing is saved.

    The form's own fields are the input: title or model name, the chosen brand (or brand_hint),
    and the attribute rows. A code the organization already has selects that category or brand.
    """
    from vimple import ai

    from .suggest import suggest_codes, write_combo_description, write_description

    if kind not in {"codes", "description", "combo_description"}:
        raise Http404
    organization, can_edit = _scope(request)
    if not can_edit:
        return HttpResponseForbidden(READ_ONLY)
    post = request.POST
    name = (post.get("title") or post.get("model_name") or post.get("name") or "").strip()
    brand = Brand.objects.filter(organization=organization, pk=_int(post.get("brand"))).first()
    brand_name = brand.name if brand else (post.get("brand_hint") or "").strip()
    attributes = attributes_from(post)
    values, message = {}, ""
    try:
        if kind == "combo_description":
            lines, _errors = read_combo_lines(post, organization)
            if not lines:
                message = "Add the products first."
            else:
                values["description"] = write_combo_description(name, lines, post.get("ai_extra"))
        elif kind == "description":
            values["description"] = write_description(name, brand_name, attributes, post.get("ai_extra"))
        else:
            values = suggest_codes(organization, name, brand_name, attributes)
            notes = []
            for field, Model, label in (("category", Category, "category"), ("brand", Brand, "brand")):
                code = values.get(f"{field}_code")
                if not code:
                    continue
                row = Model.objects.filter(organization=organization, code=code).first()
                if row is not None:
                    values[field] = str(row.pk)
                else:
                    notes.append(f"new {label} {code} · {values.get(f'{field}_name', '')} (add it with +)")
            if notes:
                message = "Suggested " + "; ".join(notes) + "."
    except ai.SuggestionFailed:
        values, message = {}, AI_FAILED
    response = HttpResponse(status=204)
    response["HX-Trigger"] = json.dumps({"ai-suggested": {"values": values, "message": message}})
    return response


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
    """Category and brand codes by id, for the live SKU preview; brand names, left out of a suggested model code."""
    brands = list(Brand.objects.filter(organization=organization).only("code", "name"))
    return {
        "categories": {str(row.pk): row.code for row in Category.objects.filter(organization=organization)},
        "brands": {str(row.pk): row.code for row in brands},
        "brand_names": {str(row.pk): row.name for row in brands},
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
    if request.method == "POST" and request.POST.get("action") == "delete":
        return deletion.delete(request, product, product_blocked(product), request.path, "catalog", product.sku)
    if request.method == "POST" and request.POST.get("action") == "archive":
        product.status = MasterProduct.Status.ARCHIVED
        product.save(update_fields=["status", "updated_at"])
        messages.success(request, f"{product.sku} archived.")
        return redirect("catalog_product", pk=product.pk)
    form = ProductForm(instance=product)
    attributes = attribute_rows(product.attributes)
    attributes_error = ""
    pictures_error = ""
    pictures = list(product.images.filter(organization=organization).values_list("url", flat=True))
    pictures_text = "\n".join(pictures or [product.image_url])
    if request.method == "POST" and request.POST.get("action") == "pictures":
        pictures_text = request.POST.get("pictures") or ""
        try:
            set_images(product, pictures_text.splitlines())
        except ValidationError as error:
            pictures_error = " ".join(error.messages)
        else:
            return redirect(f"{reverse('catalog_product', args=[product.pk])}?saved=1")
    elif request.method == "POST":
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
            "pictures": pictures,
            "pictures_text": pictures_text,
            "package_flags": {name: True for name in package_missing(product)},
            "pictures_error": pictures_error,
            "variants": product.variants().order_by("sku"),
            "listings": listings,
            "can_see_costs": can_see_costs,
            "costs": costs,
            "lines": lines,
            "saved": request.GET.get("saved") == "1",
            "can_edit": can_edit,
            "delete": deletion.card(
                "This product",
                product_blocked(product),
                ["Its pictures go with it.", unlinked(product)],
                archive=product.status != MasterProduct.Status.ARCHIVED,
            ) if can_edit else None,
            "active_tab": "products",
        },
    )


def _code_bulk(kind, can_edit):
    return deletion.bulk(kind, deletion.DELETE_ACTION, fields={"kind": kind}) if can_edit else None


@require_app(AppAccess.App.CATALOG)
def codes(request):
    """Categories and brands: add one, or change a name or an unused code."""
    organization, can_edit = _scope(request)
    if request.method == "POST" and not can_edit:
        return HttpResponseForbidden(READ_ONLY)
    forms = {kind: Form(prefix=kind, instance=Model(organization=organization)) for kind, (Model, Form, *_) in _KINDS.items()}
    editing = {}
    if request.method == "POST" and request.POST.get("kind") in _KINDS and request.POST.get("action") == "delete_selected":
        Model = _KINDS[request.POST["kind"]][0]
        return deletion.delete_selected(
            request, Model.objects.filter(organization=organization), code_blocked, lambda row: row.name, "catalog_codes"
        )
    if request.method == "POST" and request.POST.get("kind") in _KINDS and request.POST.get("action") == "delete":
        Model = _KINDS[request.POST["kind"]][0]
        row = get_object_or_404(Model, organization=organization, pk=request.POST.get("id"))
        return deletion.delete(request, row, code_blocked(row), "catalog_codes", "catalog_codes", row.name)
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
                ("category", "Categories", "Perfumes", "PERF", categories, forms["category"], editing.get("category"),
                 _code_bulk("category", can_edit)),
                ("brand", "Brands", "Only", "ONLY", brands, forms["brand"], editing.get("brand"),
                 _code_bulk("brand", can_edit)),
            ],
            "can_edit": can_edit,
            "active_tab": "codes",
        },
    )


def _stock(organization):
    """{product_id: units in the Depósito}, in one query."""
    from inventory.ledger import OWN, on_hand_by_product

    return {product_id: max(0, totals.get(OWN, 0)) for product_id, totals in on_hand_by_product(organization).items()}


@require_app(AppAccess.App.CATALOG)
def combos(request):
    organization, can_edit = _scope(request)
    if request.method == "POST":
        if not can_edit:
            return HttpResponseForbidden(READ_ONLY)
        everything = Combo.objects.filter(organization=organization)
        if request.POST.get("action") == "archive_selected":
            return deletion.archive_selected(request, everything, "catalog_combos")
        if request.POST.get("action") == "delete_selected":
            return deletion.delete_selected(request, everything, combo_blocked, lambda row: row.sku, "catalog_combos")
        return redirect("catalog_combos")
    rows = list(Combo.objects.filter(organization=organization).prefetch_related("items__product"))
    stock = _stock(organization)
    for row in rows:
        row.can_make_now = row.can_make(stock)
    return render(request, "catalog/combos.html", {
        "combos": rows, "can_edit": can_edit, "active_tab": "combos",
        "bulk": deletion.bulk("combos", deletion.ARCHIVE_ACTION, deletion.DELETE_ACTION) if can_edit else None,
    })


@require_app(AppAccess.App.CATALOG)
def combo_edit(request, pk=None):
    """A combo and its lines: which products, how many of each. Lines name products by SKU."""
    organization, can_edit = _scope(request)
    combo = get_object_or_404(Combo, organization=organization, pk=pk) if pk is not None else None
    if request.method == "POST" and not can_edit:
        return HttpResponseForbidden(READ_ONLY)
    if request.method == "POST" and combo and request.POST.get("action") == "delete":
        return deletion.delete(request, combo, combo_blocked(combo), request.path, "catalog_combos", combo.sku)
    if request.method == "POST" and combo and request.POST.get("action") == "archive":
        combo.status = Combo.Status.ARCHIVED
        combo.save(update_fields=["status", "updated_at"])
        messages.success(request, f"{combo.sku} archived.")
        return redirect("catalog_combo", pk=combo.pk)
    instance = combo or Combo(organization=organization)
    form = ComboForm(instance=instance)
    lines = [(item.product, item.quantity) for item in combo.items.select_related("product")] if combo else []
    line_errors = []
    if request.method == "POST":
        form = ComboForm(request.POST, instance=instance)
        lines, line_errors = read_combo_lines(request.POST, organization)
        if form.is_valid() and not line_errors:
            with transaction.atomic():
                saved = form.save()
                saved.items.all().delete()
                ComboItem.objects.bulk_create(
                    [
                        ComboItem(organization=organization, combo=saved, product=product, quantity=quantity)
                        for product, quantity in lines
                    ]
                )
            return redirect(f"{reverse('catalog_combo', args=[saved.pk])}?saved=1")
    stock = _stock(organization)
    rows = [{"product": product, "quantity": quantity, "stock": stock.get(product.pk, 0)} for product, quantity in lines]
    can_make = min((row["stock"] // row["quantity"] for row in rows), default=0) if rows else 0
    products = (
        MasterProduct.objects.filter(organization=organization)
        .exclude(status=MasterProduct.Status.ARCHIVED)
        .order_by("sku")
        .only("sku", "title", "list_price")
    )
    return render(
        request,
        "catalog/combo.html",
        {
            "combo": combo,
            "form": form,
            "lines": rows,
            "line_errors": line_errors,
            "can_make": can_make,
            "products": products,
            "package_flags": {name: True for name in package_missing(instance)},
            "ai": can_edit and ai_enabled(),
            "saved": request.GET.get("saved") == "1",
            "can_edit": can_edit,
            "delete": deletion.card(
                "This combo",
                combo_blocked(combo),
                ["Its lines go with it.", unlinked(combo)],
                archive=combo.status != Combo.Status.ARCHIVED,
            ) if can_edit and combo else None,
            "active_tab": "combos",
        },
    )


@require_app(AppAccess.App.CATALOG)
def copy_blocks(request):
    """Saved listing copy, grouped by kind: written once, placed in many descriptions."""
    organization, can_edit = _scope(request)
    if request.method == "POST":
        if not can_edit:
            return HttpResponseForbidden(READ_ONLY)
        if request.POST.get("action") == "delete_selected":
            return deletion.delete_selected(
                request, CopyBlock.objects.filter(organization=organization), None, lambda row: row.title, "catalog_copy"
            )
        return redirect("catalog_copy")
    rows = sorted(CopyBlock.objects.filter(organization=organization).select_related("category"), key=CopyBlock.kind_rank)
    groups = []
    for kind, label in CopyBlock.Kind.choices:
        members = [row for row in rows if row.kind == kind]
        if members:
            groups.append((label, members))
    groups.sort(key=lambda group: CopyBlock.KIND_ORDER.index(group[1][0].kind))
    return render(request, "catalog/copy.html", {
        "groups": groups, "can_edit": can_edit, "active_tab": "copy",
        "bulk": deletion.bulk("copy", deletion.DELETE_ACTION) if can_edit else None,
    })


@require_app(AppAccess.App.CATALOG)
def copy_block_edit(request, pk=None):
    organization, can_edit = _scope(request)
    block = get_object_or_404(CopyBlock, organization=organization, pk=pk) if pk is not None else None
    if request.method == "POST" and not can_edit:
        return HttpResponseForbidden(READ_ONLY)
    if request.method == "POST" and block and request.POST.get("action") == "delete":
        # Drafts that placed it skip it from now on; posted listings keep the text they were posted with.
        return deletion.delete(request, block, "", request.path, "catalog_copy", block.title)
    form = CopyBlockForm(request.POST or None, organization=organization,
                         instance=block or CopyBlock(organization=organization))
    if request.method == "POST" and form.is_valid():
        saved = form.save()
        messages.success(request, f"“{saved.title}” saved.")
        return redirect("catalog_copy")
    if not can_edit:
        for field in form.fields.values():
            field.disabled = True
    return render(
        request,
        "catalog/copy_block.html",
        {
            "block": block,
            "form": form,
            "can_edit": can_edit,
            "delete": deletion.card(
                "This copy",
                consequences=["Drafts not posted yet leave it out of their description.",
                              "Listings already posted keep their text."],
            ) if can_edit and block else None,
            "active_tab": "copy",
        },
    )
