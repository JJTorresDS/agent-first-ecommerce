import re

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
            attributes = posted_attributes(request.POST)
            try:
                validate_attributes(attributes)
            except ValidationError as error:
                attributes_error = " ".join(error.messages)
        if form.is_valid() and not attributes_error:
            saved = form.save(commit=False)
            if regular:
                saved.attributes = attributes
                if draft.product_id and request.POST.get("save_to_product"):
                    _save_to_product(draft.product, attributes)
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


# Sent from the product (brand, model, GTIN) or from their own fields: not asked in the guided form.
NOT_GUIDED = {"BRAND", "MODEL", "GTIN", "EMPTY_GTIN_REASON"}
NUMBER_AND_UNIT = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*(.*?)\s*$")


def posted_attributes(post):
    """A draft's attributes from its form: the guided fields and the free rows, empty ones left out.

    A number_unit field posts its number as attr_value and its unit as unit_<ID>: "30" + "mL" is "30 mL".
    """
    attributes = {}
    for key, value in attributes_from(post).items():
        if isinstance(value, str) and not value:
            continue
        unit = (post.get(f"unit_{key}") or "").strip()
        if unit and not isinstance(value, (bool, list)):
            value = f"{value} {unit}"
        attributes[key] = value
    return attributes


def guided_fields(draft, attributes):
    """The category's attributes as form fields: (required, recommended, free rows).

    Each field: the schema item plus its value (the draft's, else the product's), and for number_unit its
    number and unit. Free rows are the draft's attributes the guided fields don't cover.
    """
    from .drafts import OWN_ATTRIBUTES

    listed = draft.listed()
    product_values = dict(getattr(listed, "attributes", None) or {})
    required, recommended, guided = [], [], set()
    for item in draft.category_schema or []:
        if item.get("hidden") or item["id"] in OWN_ATTRIBUTES or item["id"] in NOT_GUIDED:
            continue
        guided.add(item["id"])
        value = attributes.get(item["id"], product_values.get(item["id"], ""))
        text = attribute_rows({"value": value})[0][1]
        field = {**item, "value": text, "number": "", "unit": ""}
        if item.get("type") == "number_unit" and item.get("units"):
            match = NUMBER_AND_UNIT.match(text)
            if match:
                field["number"] = match.group(1)
                field["unit"] = next((unit for unit in item["units"] if unit.lower() == match.group(2).lower()), "")
            field["unit"] = field["unit"] or item.get("default_unit") or item["units"][0]
        if item.get("values"):
            field["choice"] = next((option for option in item["values"] if option.lower() == text.lower()), "")
        (required if item.get("required") else recommended).append(field)
    free = {key: value for key, value in attributes.items() if key not in guided}
    return required, recommended, attribute_rows(free)


def copy_sources(organization, draft):
    """This organization's listings in the draft's category to copy from: the template first, then newest."""
    from .models import ListingTemplate, MeliListing

    if not draft.category_id or not draft.category_schema:
        return []
    template = (
        ListingTemplate.objects.filter(organization=organization, category_id=draft.category_id)
        .exclude(state=ListingTemplate.State.FAILED)
        .values_list("item_id", flat=True)
        .first()
    )
    rows = (
        MeliListing.objects.filter(organization=organization, category_id=draft.category_id)
        .exclude(item_id=draft.item_id or "-")
        .only("pk", "item_id", "title", "seller_sku", "status", "thumbnail", "meli_updated_at")
        .order_by("-meli_updated_at", "item_id")[:60]
    )
    sources, seen = [], set()
    for row in rows:
        if row.item_id in seen:
            continue
        seen.add(row.item_id)
        row.is_template = row.item_id == template
        sources.append(row)
    sources.sort(key=lambda row: not row.is_template)
    return sources[:30]


def _attributes_context(organization, draft, attributes, error, **extra):
    """The regular draft's attributes: Mercado Libre's own fields for its category, then free rows."""
    from vimple.ai import enabled

    from .drafts import listed_as_generic

    if "missing" in extra:
        missing, wrong = extra.pop("missing"), extra.pop("wrong")
    else:
        missing, wrong = _problems(organization, draft)
    copied = extra.pop("copied", {})
    required, recommended, free = guided_fields(draft, attributes)
    for field in required + recommended:
        if field["id"] in copied:
            field["copied"] = copied[field["id"]]
    ai = enabled()
    return {
        "copy_sources": copy_sources(organization, draft) if ai and not draft.template else [],
        "attributes": free,
        "guided_required": required,
        "guided_recommended": recommended,
        "missing": missing,
        "wrong": wrong,
        "generic": listed_as_generic(draft),
        "attributes_error": error,
        "ai": ai,
        **extra,
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
    attributes = posted_attributes(request.POST)
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
    rows = {**attributes, **{item["id"]: filled[item["id"]] for item in asked if item["id"] in filled}}
    return render(
        request,
        "meli/_draft_attributes.html",
        {"draft": draft, "can_edit": True,
         **_attributes_context(organization, draft, rows, "", ai_note=note, missing=missing, wrong=wrong)},
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


def _save_to_product(product, attributes):
    """The draft's attributes become the product's too, so its next listing starts complete.

    Listing-only values (brand Generic, the GTIN reason, SKU, package) stay on the draft.
    """
    from .drafts import OWN_ATTRIBUTES

    kept = {key: value for key, value in attributes.items() if key not in OWN_ATTRIBUTES and key not in NOT_GUIDED}
    merged = {**(product.attributes or {}), **kept}
    if merged == product.attributes:
        return
    validate_attributes(merged)
    product.attributes = merged
    product.save(update_fields=["attributes", "updated_at"])


@require_app(AppAccess.App.CATALOG)
@require_POST
def meli_draft_copy(request, pk):
    """Values for the draft's empty attributes, adapted by AI from one of this organization's listings.

    Reads the listing as stored by the sync: no Mercado Libre call. Nothing is saved.
    """
    from catalog.suggest import adapt_attributes
    from vimple import ai

    from .drafts import OWN_ATTRIBUTES
    from .models import MeliListing

    organization, can_edit = _scope(request)
    if not can_edit:
        return HttpResponseForbidden(READ_ONLY)
    draft = get_object_or_404(
        ListingDraft.objects.select_related("product__product_model__brand", "combo"), organization=organization, pk=pk
    )
    if draft.locked() or not draft.regular():
        return HttpResponseForbidden("Posted drafts can't change.")
    source = get_object_or_404(MeliListing, organization=organization, pk=request.POST.get("listing"))
    attributes = posted_attributes(request.POST)
    draft.attributes = attributes
    schema = {item["id"]: item for item in draft.category_schema or []}
    listed = draft.listed()
    current = {**(getattr(listed, "attributes", None) or {}), **attributes}
    offered = []
    for attribute in source.payload.get("attributes") or []:
        key = str(attribute.get("id") or "") if isinstance(attribute, dict) else ""
        value = " ".join(str(attribute.get("value_name") or "").split()) if key else ""
        # Only what this category asks for and the draft doesn't have yet; never the other item's identity.
        if (not value or key not in schema or key in OWN_ATTRIBUTES or key in NOT_GUIDED
                or schema[key].get("hidden") or current.get(key) not in (None, "")):
            continue
        offered.append({"id": key, "name": str(attribute.get("name") or schema[key]["name"])[:100], "value": value[:200]})
    copied, dropped, note = {}, {}, ""
    if not offered:
        note = f"Nothing to copy from “{source.title}”: it has no values for the fields still empty here."
    else:
        brand, _model = brand_and_model(listed)
        model = getattr(listed, "product_model", None)
        target = {
            "title": listed.title[:300],
            "brand": brand,
            "description": ((model.description if model else listed.description) or "")[:1500],
            "attributes": current,
        }
        try:
            copied, dropped = adapt_attributes(source.title, offered, target, schema)
        except ai.SuggestionFailed:
            note = "The copy didn't come through; try again or fill them in yourself."
    names = {item["id"]: item["name"] for item in offered}
    rows = {**attributes, **copied}
    return render(
        request,
        "meli/_draft_attributes.html",
        {
            "draft": draft,
            "can_edit": True,
            "copy_note": note,
            "copy_dropped": [(names[key], reason) for key, reason in dropped.items() if key in names],
            "copy_count": len(copied),
            "copy_source": source.title,
            **_attributes_context(organization, draft, rows, "", copied={key: source.title for key in copied}),
        },
    )
