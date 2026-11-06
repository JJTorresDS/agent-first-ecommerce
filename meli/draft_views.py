from django.core.exceptions import ValidationError
from django.http import HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.access import require_app
from accounts.models import AppAccess, can_edit_products, membership_for
from catalog.forms import attribute_rows, attributes_from
from catalog.validators import validate_attributes

from .drafts import (
    package_missing,
    available_for,
    brand_and_model,
    defaults_for,
    inventory_started,
    queue_for_posting,
    stock_by_product,
)
from .forms import CatalogDraftForm, DraftForm, ListingDefaultsForm
from .models import ListingDraft, MercadoLibreAccount

READ_ONLY = "Only owners and admins can create Mercado Libre listings."


def _scope(request):
    membership = membership_for(request.user)
    return membership.organization, can_edit_products(membership)


@require_app(AppAccess.App.CATALOG)
def meli_drafts(request):
    """New Mercado Libre listings: the defaults, add from a catalog URL, and post the ready ones."""
    from .jobs import dispatch_drafts

    organization, can_edit = _scope(request)
    if request.method == "POST" and not can_edit:
        return HttpResponseForbidden(READ_ONLY)
    defaults = defaults_for(organization)
    stock = stock_by_product(organization)
    started = inventory_started(organization)
    connected = MercadoLibreAccount.objects.filter(organization=organization).exists()
    defaults_form = ListingDefaultsForm(instance=defaults, prefix="defaults")
    add_form = CatalogDraftForm(organization=organization, stock=stock, prefix="add")
    action = request.POST.get("action") if request.method == "POST" else ""
    if action == "defaults":
        defaults_form = ListingDefaultsForm(request.POST, instance=defaults, prefix="defaults")
        if defaults_form.is_valid():
            defaults_form.save()
            # Drafts that follow the defaults were checked with the old ones.
            _recheck(organization, ListingDraft.objects.filter(organization=organization, use_defaults=True))
            return redirect("meli_drafts")
    elif action == "add" and started and connected:
        add_form = CatalogDraftForm(request.POST, organization=organization, stock=stock, prefix="add")
        if add_form.is_valid():
            draft = ListingDraft.objects.create(
                organization=organization,
                kind=ListingDraft.Kind.CATALOG,
                product=add_form.cleaned_data["product"],
                catalog_url=add_form.cleaned_data["url"],
                catalog_product_id=add_form.catalog_product_id,
                price=add_form.cleaned_data["price"],
                quantity=add_form.cleaned_data["quantity"],
                status=ListingDraft.Status.CHECKING,
                created_by=request.user,
            )
            dispatch_drafts(organization, "check", [draft.pk])
            return redirect("meli_drafts")
    elif action in {"post", "post_all"}:
        rows = ListingDraft.objects.filter(organization=organization, status=ListingDraft.Status.READY)
        if action == "post":
            rows = rows.filter(pk__in=_ids(request.POST.getlist("draft")))
        queued = queue_for_posting(organization, list(rows.values_list("pk", flat=True)))
        dispatch_drafts(organization, "publish", queued)
        return redirect("meli_drafts")
    drafts = list(
        ListingDraft.objects.filter(organization=organization).select_related("product", "combo").prefetch_related(
            "combo__items"
        )
    )
    for draft in drafts:
        draft.stock = available_for(draft, stock)
    context = {
        "drafts": drafts,
        "busy": any(draft.status in ListingDraft.BUSY for draft in drafts),
        "ready_count": sum(draft.status == ListingDraft.Status.READY for draft in drafts),
        "defaults_form": defaults_form,
        "add_form": add_form,
        "started": started,
        "connected": connected,
        "can_edit": can_edit,
        "active_tab": "meli_new",
    }
    if request.GET.get("poll") == "drafts" and request.headers.get("HX-Request") == "true":
        return render(request, "meli/_drafts.html", context)
    return render(request, "meli/drafts.html", context)


@require_app(AppAccess.App.CATALOG)
def meli_draft(request, pk):
    """One draft: price, quantity, and the defaults or its own settings. Saving checks it again."""
    from .jobs import dispatch_drafts

    organization, can_edit = _scope(request)
    draft = get_object_or_404(
        ListingDraft.objects.select_related("product__product_model__brand", "combo"), organization=organization, pk=pk
    )
    if request.method == "POST" and not can_edit:
        return HttpResponseForbidden(READ_ONLY)
    regular = draft.regular()
    attributes = dict(draft.attributes or {})
    attributes_error = ""
    available = available_for(draft, stock_by_product(organization))
    defaults = defaults_for(organization)
    initial = {}
    if draft.use_defaults:
        # Unticking "Use the defaults" starts from them.
        initial = {name: getattr(defaults, name) for name in ListingDraft.SETTING_FIELDS}
    form = DraftForm(instance=draft, initial=initial, available=available)
    if request.method == "POST" and not draft.locked():
        if request.POST.get("action") == "delete":
            draft.delete()
            return redirect("meli_drafts")
        form = DraftForm(request.POST, instance=draft, available=available)
        if regular:
            attributes = attributes_from(request.POST)
            try:
                validate_attributes(attributes)
            except ValidationError as error:
                attributes_error = " ".join(error.messages)
        if form.is_valid() and not attributes_error:
            saved = form.save(commit=False)
            if regular:
                saved.attributes = attributes
            saved.status = ListingDraft.Status.CHECKING
            saved.error = ""
            saved.error_details = {}
            saved.save()
            dispatch_drafts(organization, "check", [saved.pk])
            return redirect("meli_drafts")
    return render(
        request,
        "meli/draft.html",
        {
            "draft": draft,
            "form": form,
            "defaults": defaults,
            "available": available,
            "can_edit": can_edit and not draft.locked(),
            **(_attributes_context(organization, draft, attributes, attributes_error) if regular else {}),
            "active_tab": "meli_new",
        },
    )


def _attributes_context(organization, draft, attributes, error):
    """The regular draft's own attributes, with an empty row for each one Mercado Libre still needs."""
    from vimple.ai import enabled

    missing, wrong = _problems(organization, draft)
    rows = {**attributes, **{item["id"]: attributes.get(item["id"], "") for item in missing}}
    from .drafts import listed_as_generic

    return {
        "attributes": attribute_rows(rows),
        "missing": missing,
        "wrong": wrong,
        "generic": listed_as_generic(draft),
        "attributes_error": error,
        "ai": enabled(),
    }


def _recheck(organization, rows):
    from .jobs import dispatch_drafts

    rows = rows.exclude(status__in=ListingDraft.LOCKED).exclude(status=ListingDraft.Status.CHECKING)
    ids = list(rows.values_list("pk", flat=True))
    ListingDraft.objects.filter(pk__in=ids).update(status=ListingDraft.Status.CHECKING, error="", error_details={})
    dispatch_drafts(organization, "check", ids)


def _ids(values):
    ids = []
    for value in values:
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            continue
    return ids


PACKAGE_FIRST = "Fill in the package weight and size first."


def _actions_context(organization, listed, can_edit, **objects):
    reason = _listable(organization, listed)
    return {**objects, "can_edit": can_edit, "reason": reason, "package_first": reason == PACKAGE_FIRST}


def _listable(organization, listed):
    """Why this product or combo can't get a listing yet, or "" when it can."""
    from catalog.models import Combo

    if not MercadoLibreAccount.objects.filter(organization=organization).exists():
        return "Connect Mercado Libre to list it."
    if not inventory_started(organization):
        return "Count your stock first to list it."
    if not listed.list_price:
        return "Set a price first."
    if package_missing(listed):
        return PACKAGE_FIRST
    stock = stock_by_product(organization)
    if isinstance(listed, Combo):
        if listed.can_make(stock) < 1:
            return "Not enough stock in the Depósito to make one."
    elif stock.get(listed.pk, 0) < 1:
        return "No stock in the Depósito to list."
    return ""


@require_app(AppAccess.App.CATALOG)
def meli_product_actions(request, pk):
    """The product page's Mercado Libre buttons, loaded by htmx so catalog never imports meli."""
    from catalog.models import MasterProduct

    organization, can_edit = _scope(request)
    product = get_object_or_404(MasterProduct, organization=organization, pk=pk)
    return render(
        request,
        "meli/_product_actions.html",
        _actions_context(organization, product, can_edit, product=product),
    )


@require_app(AppAccess.App.CATALOG)
@require_POST
def meli_draft_from_product(request, pk):
    """A draft for one product: a regular listing, or in its catalog when it came from one."""
    from catalog.models import MasterProduct

    from .jobs import dispatch_drafts

    organization, can_edit = _scope(request)
    if not can_edit:
        return HttpResponseForbidden(READ_ONLY)
    product = get_object_or_404(MasterProduct, organization=organization, pk=pk)
    reason = _listable(organization, product)
    if reason:
        return HttpResponseBadRequest(reason)
    in_catalog = request.POST.get("kind") == "catalog" and product.meli_catalog_product_id
    draft = ListingDraft(
        organization=organization,
        kind=ListingDraft.Kind.CATALOG if in_catalog else ListingDraft.Kind.PRODUCT,
        product=product,
        price=product.list_price,
        quantity=1,
        category_id=product.meli_category_id,
        created_by=request.user,
    )
    if in_catalog:
        draft.catalog_product_id = product.meli_catalog_product_id
        draft.catalog_url = f"https://www.mercadolibre.com.ar/p/{product.meli_catalog_product_id}"
    draft.status = ListingDraft.Status.CHECKING
    draft.save()
    dispatch_drafts(organization, "check", [draft.pk])
    return redirect("meli_draft", pk=draft.pk)


@require_app(AppAccess.App.CATALOG)
@require_POST
def meli_draft_ai(request, pk):
    """AI values for the attributes a regular draft is missing, shown in its editor. Nothing is saved."""
    from catalog.suggest import fill_attributes
    from vimple import ai

    organization, can_edit = _scope(request)
    if not can_edit:
        return HttpResponseForbidden(READ_ONLY)
    draft = get_object_or_404(
        ListingDraft.objects.select_related("product__product_model__brand", "combo"), organization=organization, pk=pk
    )
    if draft.locked():
        return HttpResponseForbidden("Posted drafts can't change.")
    attributes = attributes_from(request.POST)
    draft.attributes = attributes
    missing, wrong = _problems(organization, draft)
    asked = _to_fill(organization, draft)
    note = ""
    try:
        listed = draft.listed()
        brand, _model = brand_and_model(listed)
        filled = fill_attributes(listed.title, brand, {**listed.attributes, **attributes}, asked)
    except ai.SuggestionFailed:
        filled, note = {}, "The suggestion didn't come through; fill them in yourself."
    if asked and not filled and not note:
        note = "No confident values: fill them in yourself."
    rows = {**attributes, **{item["id"]: filled.get(item["id"], attributes.get(item["id"], "")) for item in asked}}
    return render(
        request,
        "meli/_draft_attributes.html",
        {"draft": draft, "attributes": attribute_rows(rows), "missing": missing, "wrong": wrong, "ai_note": note,
         "can_edit": True, "ai": True},
    )


def _to_fill(organization, draft):
    from .drafts import attributes_to_fill, build_item, settings_of

    if not draft.regular():
        return []
    return attributes_to_fill(draft, build_item(draft, settings_of(draft, defaults_for(organization))))


def _problems(organization, draft):
    """(missing, wrong) attributes of a regular draft: what Mercado Libre needs, and values not on its list."""
    from .drafts import build_item, invalid_values, missing_attributes, settings_of

    if not draft.regular():
        return [], []
    body = build_item(draft, settings_of(draft, defaults_for(organization)))
    return missing_attributes(draft, body), invalid_values(draft, body)


@require_app(AppAccess.App.CATALOG)
def meli_combo_actions(request, pk):
    """The combo page's Mercado Libre button, loaded by htmx so catalog never imports meli."""
    from catalog.models import Combo

    organization, can_edit = _scope(request)
    combo = get_object_or_404(Combo, organization=organization, pk=pk)
    return render(
        request,
        "meli/_combo_actions.html",
        _actions_context(organization, combo, can_edit, combo=combo),
    )


@require_app(AppAccess.App.CATALOG)
@require_POST
def meli_draft_from_combo(request, pk):
    """A regular listing draft for one combo, with SELLER_SKU = the combo's SKU."""
    from catalog.models import Combo

    from .jobs import dispatch_drafts

    organization, can_edit = _scope(request)
    if not can_edit:
        return HttpResponseForbidden(READ_ONLY)
    combo = get_object_or_404(Combo, organization=organization, pk=pk)
    reason = _listable(organization, combo)
    if reason:
        return HttpResponseBadRequest(reason)
    draft = ListingDraft(
        organization=organization,
        kind=ListingDraft.Kind.COMBO,
        combo=combo,
        price=combo.list_price,
        quantity=1,
        category_id=combo.meli_category_id,
        created_by=request.user,
    )
    draft.status = ListingDraft.Status.CHECKING
    draft.save()
    dispatch_drafts(organization, "check", [draft.pk])
    return redirect("meli_draft", pk=draft.pk)
