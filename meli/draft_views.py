import re

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from accounts.access import require_app
from accounts.models import AppAccess, can_edit_products, membership_for
from catalog.forms import attribute_rows, attributes_from
from catalog.validators import validate_attributes
from vimple.ai import enabled as ai_enabled

from .description import LIMIT, blocks_for, compose, default_parts, parts_from
from .description import warnings as description_warnings
from catalog.models import PACKAGE_FIELDS

from .drafts import (
    LOCKED_KEYS,
    PACKAGE,
    build_item,
    package_attributes,
    description_of,
    package_missing,
    available_for,
    brand_and_model,
    defaults_for,
    inventory_started,
    queue_for_posting,
    stock_by_product,
)
from .forms import CatalogDraftForm, DraftForm, ListingDefaultsForm
from .models import ListingDraft, ListingTemplate, MercadoLibreAccount, MeliListing

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
    elif action == "delete":
        # Drafts a job is posting right now stay; a posted listing stays on Mercado Libre either way.
        rows = ListingDraft.objects.filter(organization=organization, pk__in=_ids(request.POST.getlist("draft"))).exclude(
            status__in=(ListingDraft.Status.QUEUED, ListingDraft.Status.PUBLISHING)
        )
        count, _ = rows.delete()
        messages.success(request, f"{count} draft{'s' if count != 1 else ''} deleted.")
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
        "templates": ListingTemplate.objects.filter(
            organization=organization, state=ListingTemplate.State.READY
        ).order_by("category_id"),
        "ai": ai_enabled(),
        # One way to start at a time: the other forms stay behind their tabs.
        "start": "link" if action == "add" else (
            request.GET.get("start") if request.GET.get("start") in {"product", "template", "link"} else "product"
        ),
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
    parts = draft.description_parts or [{"product": True}]
    if request.method == "POST" and not draft.locked():
        if request.POST.get("action") == "delete":
            draft.delete()
            messages.success(request, f"Draft #{pk} deleted.")
            return redirect("meli_drafts")
        form = DraftForm(request.POST, instance=draft, available=available)
        if regular:
            if request.POST.get("desc_present"):
                parts = parts_from(request.POST, organization)
            attributes = posted_attributes(request.POST)
            try:
                validate_attributes(attributes)
            except ValidationError as error:
                attributes_error = " ".join(error.messages)
        if form.is_valid() and not attributes_error:
            saved = form.save(commit=False)
            if regular:
                saved.attributes = attributes
                saved.description_parts = parts
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
            **(_fix_context(draft) if fix_offered(draft, can_edit and not draft.locked()) else {}),
            "fix_count": sum(
                len(value) if key == "attributes" and isinstance(value, dict) else 1
                for key, value in (draft.overrides or {}).items()
            ),
            **(_attributes_context(organization, draft, attributes, attributes_error, copy_pending=_copy_pending(request))
               if regular else {}),
            **(_description_context(organization, draft, parts) if regular else {}),
            "active_tab": "meli_new",
        },
    )


def _description_context(organization, draft, parts):
    """The composer: the copy on offer, the pieces in order, and the text they make."""
    from catalog.models import CopyBlock

    listed = draft.listed()
    offered = blocks_for(organization, listed)
    by_id = {row.pk: row for row in offered}
    missing = [part["block"] for part in parts if "block" in part and part["block"] not in by_id]
    # A block placed earlier that no longer fits the category still shows; it's this organization's.
    for row in CopyBlock.objects.filter(organization=organization, pk__in=missing):
        by_id[row.pk] = row
    pieces = []
    for part in parts:
        if part.get("product"):
            pieces.append({"type": "product"})
        elif "block" in part and part["block"] in by_id:
            pieces.append({"type": "block", "block": by_id[part["block"]]})
        elif "text" in part:
            pieces.append({"type": "text", "text": part["text"]})
    preview = compose(ListingDraft(organization=organization, product=draft.product, combo=draft.combo,
                                   description_parts=parts or [{"product": True}]), by_id)
    return {
        "copy_blocks": offered,
        "copy_kinds": CopyBlock.Kind.choices,
        "pieces": pieces,
        "product_text": description_of(listed),
        "copy_bodies": {str(row.pk): row.body for row in by_id.values()} | {"product": description_of(listed)},
        "preview": preview,
        "description_warnings": description_warnings(preview),
        "description_limit": LIMIT,
    }


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


def _copy_pending(request):
    """The listing a draft started from a template copies once its attributes load (?copy=<pk>)."""
    value = request.GET.get("copy") if request.method == "GET" else ""
    return int(value) if value and value.isdigit() else None


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
    template = None
    if not reason and listed.meli_category_id:
        template = ListingTemplate.objects.filter(
            organization=organization, category_id=listed.meli_category_id, state=ListingTemplate.State.READY
        ).first()
    from catalog.models import Combo

    from .listing_views import listing_counts, listing_summary

    is_combo = isinstance(listed, Combo)
    counts = listing_counts(organization, combos=[listed]) if is_combo else listing_counts(organization, [listed])
    return {**objects, "can_edit": can_edit, "reason": reason, "package_first": reason == PACKAGE_FIRST,
            "template": template, "listed": listed, "is_combo": is_combo,
            "listing_summary": listing_summary(counts.get(("combo" if is_combo else "product", listed.pk), {}))}


def _listable(organization, listed, stock=None, connected=None, started=None):
    """Why this product or combo can't get a listing yet, or "" when it can.

    A list of rows passes stock, connected and started, read once for all of them.
    """
    from catalog.models import Combo

    if connected is None:
        connected = MercadoLibreAccount.objects.filter(organization=organization).exists()
    if not connected:
        return "Connect Mercado Libre to list it."
    if not (inventory_started(organization) if started is None else started):
        return "Count your stock first to list it."
    if not listed.list_price:
        return "Set a price first."
    if package_missing(listed):
        return PACKAGE_FIRST
    if stock is None:
        stock = stock_by_product(organization)
    if isinstance(listed, Combo):
        if listed.can_make(stock) < 1:
            return "Not enough stock in the Depósito to make one."
    elif stock.get(listed.pk, 0) < 1:
        return "No stock in the Depósito to list."
    return ""


PICK_LIMIT = 20


@require_app(AppAccess.App.CATALOG)
def meli_listing_pick(request):
    """Create listing's search: this organization's products and combos, each with its button or why not yet."""
    from django.db.models import Q

    from catalog.models import Combo, MasterProduct

    organization, can_edit = _scope(request)
    query = (request.GET.get("q") or "").strip()[:100]
    template = None
    if request.GET.get("template"):
        template = get_object_or_404(
            ListingTemplate, organization=organization, pk=request.GET["template"], state=ListingTemplate.State.READY
        )
    products = (
        MasterProduct.objects.filter(organization=organization)
        .exclude(status=MasterProduct.Status.ARCHIVED)
    )
    combos = Combo.objects.filter(organization=organization).exclude(status=Combo.Status.ARCHIVED).prefetch_related(
        "items"
    )
    if query:
        products = products.filter(Q(title__icontains=query) | Q(sku__icontains=query))
        combos = combos.filter(Q(title__icontains=query) | Q(sku__icontains=query))
    rows = list(products.order_by("title")[:PICK_LIMIT]) + list(combos.order_by("title")[:PICK_LIMIT])
    if template is not None:
        # The template's own category first: its attributes fit them best.
        rows.sort(key=lambda row: row.meli_category_id != template.category_id)
    rows = rows[:PICK_LIMIT]
    stock = stock_by_product(organization)
    connected = MercadoLibreAccount.objects.filter(organization=organization).exists()
    started = inventory_started(organization)
    from .listing_views import listing_counts, listing_summary

    counts = listing_counts(
        organization,
        [row for row in rows if not isinstance(row, Combo)],
        [row for row in rows if isinstance(row, Combo)],
    )
    for row in rows:
        row.is_combo = isinstance(row, Combo)
        row.listing_summary = listing_summary(counts.get(("combo" if row.is_combo else "product", row.pk), {}))
        row.reason = _listable(organization, row, stock, connected, started)
        row.stock = row.can_make(stock) if row.is_combo else stock.get(row.pk, 0)
    return render(
        request,
        "meli/_pick.html",
        {"rows": rows, "query": query, "template": template, "can_edit": can_edit, "package_first": PACKAGE_FIRST},
    )


LISTED_LIMIT = 50
LISTED_ORDER = {"active": 0, "paused": 1}


@require_app(AppAccess.App.CATALOG)
def meli_listed(request):
    """One product's or combo's Mercado Libre listings, for the dialog behind its "2 active, 1 paused"."""
    from django.db.models import Case, Value, When

    from catalog.models import Combo, MasterProduct

    organization, _can_edit = _scope(request)
    if request.GET.get("combo"):
        listed = get_object_or_404(Combo, organization=organization, pk=request.GET["combo"])
        rows = MeliListing.objects.filter(organization=organization, combo=listed)
    else:
        listed = get_object_or_404(MasterProduct, organization=organization, pk=request.GET.get("product"))
        rows = MeliListing.objects.filter(organization=organization, product=listed)
    # Active first, then paused, then the rest.
    rank = Case(*(When(status=status, then=Value(order)) for status, order in LISTED_ORDER.items()), default=Value(2))
    rows = list(rows.order_by(rank, "-synced_at", "item_id", "variation_id")[: LISTED_LIMIT + 1])
    templates = set(
        ListingTemplate.objects.filter(
            organization=organization, item_id__in={row.item_id for row in rows}
        ).values_list("item_id", flat=True)
    )
    for row in rows:
        row.is_template = row.item_id in templates
    return render(
        request,
        "meli/_listed.html",
        {"listed": listed, "rows": rows[:LISTED_LIMIT], "more": len(rows) > LISTED_LIMIT},
    )


def _chosen_template(organization, request):
    """The template a draft starts from (Create listing → From a template), or None."""
    if not request.POST.get("template"):
        return None
    return get_object_or_404(
        ListingTemplate, organization=organization, pk=request.POST["template"], state=ListingTemplate.State.READY
    )


def _open_draft(organization, draft, template):
    """The new draft's page; from a template, it copies the template's attributes once they load."""
    url = reverse("meli_draft", args=[draft.pk])
    source = template and MeliListing.objects.filter(organization=organization, item_id=template.item_id).first()
    if source:
        url += f"?copy={source.pk}"
    return redirect(url)


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
    template = _chosen_template(organization, request)
    in_catalog = request.POST.get("kind") == "catalog" and product.meli_catalog_product_id and template is None
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
    if template is not None:
        draft.category_id = template.category_id
    if not in_catalog:
        draft.description_parts = default_parts(organization, product)
    draft.status = ListingDraft.Status.CHECKING
    draft.save()
    dispatch_drafts(organization, "check", [draft.pk])
    return _open_draft(organization, draft, template)


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
    template = _chosen_template(organization, request)
    draft = ListingDraft(
        organization=organization,
        kind=ListingDraft.Kind.COMBO,
        combo=combo,
        price=combo.list_price,
        quantity=1,
        category_id=template.category_id if template else combo.meli_category_id,
        description_parts=default_parts(organization, combo),
        created_by=request.user,
    )
    draft.status = ListingDraft.Status.CHECKING
    draft.save()
    dispatch_drafts(organization, "check", [draft.pk])
    return _open_draft(organization, draft, template)


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


FIXABLE = (ListingDraft.Status.NEEDS_FIX, ListingDraft.Status.FAILED)


def fix_offered(draft, can_edit):
    """Whether a draft's page offers an AI fix: Mercado Libre refused it, and the body and answer were kept."""
    details = draft.error_details or {}
    return bool(
        can_edit
        and ai_enabled()
        and draft.status in FIXABLE
        and isinstance(details.get("sent"), dict)
        and isinstance(details.get("response"), dict)
    )


def _fix_context(draft, fix=None, note="", text=None):
    """The AI fix card: the stored suggestion while it's for the error on screen."""
    import json

    if fix is None:
        stored = draft.ai_fix or {}
        fix = stored if stored.get("error") == draft.error and stored.get("body") else {}
    if text is None:
        text = json.dumps(fix["body"], indent=2, ensure_ascii=False) if fix else ""
    return {"draft": draft, "fix": fix, "fix_note": note, "fix_text": text, "fix_offered": True}


def _fixable(request, pk):
    organization, can_edit = _scope(request)
    if not can_edit:
        return organization, None, HttpResponseForbidden(READ_ONLY)
    draft = get_object_or_404(
        ListingDraft.objects.select_related("product__product_model__brand", "combo"), organization=organization, pk=pk
    )
    if draft.locked():
        return organization, draft, HttpResponseForbidden("Posted drafts can't change.")
    return organization, draft, None


@require_app(AppAccess.App.CATALOG)
@require_POST
def meli_draft_fix(request, pk):
    """Ask the AI for a corrected body after a refusal. Shown to review and edit; nothing is applied."""
    from django.utils import timezone

    from catalog.suggest import fix_listing
    from vimple import ai

    from .drafts import FixRefused, fix_changes, uses_user_products

    organization, draft, refused = _fixable(request, pk)
    if refused:
        return refused
    if not fix_offered(draft, True):
        return HttpResponseBadRequest("Mercado Libre hasn't refused this draft.")
    sent = draft.error_details["sent"]
    listed = draft.listed()
    brand, _model = brand_and_model(listed)
    try:
        body, notes = fix_listing(
            sent,
            draft.error_details["response"],
            draft.category_schema,
            uses_user_products(organization),
            listed.title,
            brand,
            listed.attributes,
            {name: str(getattr(listed, name)) for name in PACKAGE_FIELDS if getattr(listed, name) is not None},
        )
        # The fields Vimple owns stay as sent, whatever the AI wrote.
        body = {**{key: value for key, value in body.items() if key not in LOCKED_KEYS},
                **{key: sent[key] for key in LOCKED_KEYS if key in sent}}
        sku = next((a for a in sent.get("attributes") or [] if a.get("id") == "SELLER_SKU"), None)
        attributes = [a for a in body.get("attributes") or [] if not (isinstance(a, dict) and a.get("id") == "SELLER_SKU")]
        body["attributes"] = ([sku] if sku else []) + attributes
        changes = fix_changes(sent, body, notes)
    except ai.SuggestionFailed:
        return render(request, "meli/_ai_fix.html", _fix_context(draft, {}, "The suggestion didn't come through. Try again."))
    except FixRefused:
        return render(request, "meli/_ai_fix.html", _fix_context(draft, {}, "The AI answered with something that isn't a listing. Try again."))
    if not changes:
        return render(request, "meli/_ai_fix.html", _fix_context(draft, {}, "The AI found nothing to change. Fix it by hand, or report the error."))
    fix = {
        "error": draft.error,
        "body": body,
        "changes": changes,
        "package": any(change["field"].startswith("SELLER_PACKAGE_") for change in changes),
        "made_at": timezone.now().isoformat(),
    }
    draft.ai_fix = fix
    draft.save(update_fields=["ai_fix", "updated_at"])
    return render(request, "meli/_ai_fix.html", _fix_context(draft, fix))


@require_app(AppAccess.App.CATALOG)
@require_POST
def meli_draft_fix_apply(request, pk):
    """Keep the reviewed fix on the draft and check it again. Package values can also go to the product."""
    import json

    from .drafts import FixRefused, clean_fix, package_from_fix, settings_of, uses_user_products
    from .jobs import dispatch_drafts

    organization, draft, refused = _fixable(request, pk)
    if refused:
        return refused
    text = request.POST.get("fix_body") or ""
    try:
        edited = json.loads(text)
        base = build_item(
            draft, settings_of(draft, defaults_for(organization)), uses_user_products(organization), overrides=False
        )
        overrides = clean_fix(edited, base)
    except ValueError as error:
        note = str(error) if isinstance(error, FixRefused) else f"That isn't valid JSON: {error}"
        # Re-rendered in place (htmx), keeping what was typed.
        return render(request, "meli/_ai_fix.html", _fix_context(draft, None, note, text))
    notes = []
    if request.POST.get("save_package"):
        values, unread = package_from_fix(overrides)
        if values:
            listed = draft.listed()
            for field, value in values.items():
                setattr(listed, field, value)
            listed.save(update_fields=[*values, "updated_at"])
            # build_item sends the product's package now. A catalog product's own measures would still win,
            # so there the fix stays, written as the product's ("450 g").
            kept = draft.catalog_package or {}
            saved = {attribute["id"]: attribute for attribute in package_attributes(listed)}
            for attribute_id, field, _unit in PACKAGE:
                if field not in values:
                    continue
                if attribute_id in kept:
                    overrides["attributes"][attribute_id] = {"value_name": saved[attribute_id]["value_name"]}
                else:
                    overrides["attributes"].pop(attribute_id, None)
            if not overrides.get("attributes"):
                overrides.pop("attributes", None)
            notes.append(f"Package saved to {listed.sku}.")
        if unread:
            notes.append(f"Not saved to the product (no number and unit): {', '.join(unread)}.")
    draft.overrides = overrides
    draft.status = ListingDraft.Status.CHECKING
    draft.error = ""
    draft.error_details = {}
    draft.save(update_fields=["overrides", "status", "error", "error_details", "updated_at"])
    dispatch_drafts(organization, "check", [draft.pk])
    messages.success(request, " ".join(["Checking with the AI fix.", *notes]))
    response = HttpResponse(status=204)
    response["HX-Redirect"] = reverse("meli_draft", args=[draft.pk])
    return response


@require_app(AppAccess.App.CATALOG)
@require_POST
def meli_draft_fix_remove(request, pk):
    """Stop sending the applied fix and check the draft again."""
    from .jobs import dispatch_drafts

    organization, draft, refused = _fixable(request, pk)
    if refused:
        return refused
    draft.overrides = {}
    draft.status = ListingDraft.Status.CHECKING
    draft.error = ""
    draft.error_details = {}
    draft.save(update_fields=["overrides", "status", "error", "error_details", "updated_at"])
    dispatch_drafts(organization, "check", [draft.pk])
    messages.success(request, "AI fix removed. Checking again.")
    return redirect("meli_draft", pk=draft.pk)
