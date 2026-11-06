from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render

from accounts.access import require_app
from accounts.models import AppAccess, can_edit_products, membership_for

from .drafts import defaults_for, inventory_started, queue_for_posting, stock_by_product
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
    drafts = list(ListingDraft.objects.filter(organization=organization).select_related("product"))
    for draft in drafts:
        draft.stock = stock.get(draft.product_id, 0)
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
    draft = get_object_or_404(ListingDraft.objects.select_related("product"), organization=organization, pk=pk)
    if request.method == "POST" and not can_edit:
        return HttpResponseForbidden(READ_ONLY)
    available = stock_by_product(organization).get(draft.product_id, 0)
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
        if form.is_valid():
            saved = form.save(commit=False)
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
            "active_tab": "meli_new",
        },
    )


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
