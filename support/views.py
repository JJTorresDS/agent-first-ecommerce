from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from accounts.access import require_app
from accounts.models import AppAccess, membership_for

from .models import ErrorReport
from .reports import report


@require_app(AppAccess.App.CATALOG)
@require_POST
def report_draft_error(request, pk):
    """Store what went wrong on a listing draft: Mercado Libre's answer, what was sent, and its settings."""
    from meli.drafts import defaults_for, settings_of, uses_user_products
    from meli.models import ListingDraft

    organization = membership_for(request.user).organization
    draft = get_object_or_404(ListingDraft.objects.select_related("product", "combo"), organization=organization, pk=pk)
    if not draft.error_details:
        return HttpResponseBadRequest("This draft has nothing to report.")
    settings = settings_of(draft, defaults_for(organization))
    context = {
        "draft": {
            "id": draft.pk,
            "kind": draft.kind,
            "status": draft.status,
            "price": str(draft.price),
            "quantity": draft.quantity,
            "category_id": draft.category_id,
            "catalog_product_id": draft.catalog_product_id,
            "item_id": draft.item_id,
            "attributes": draft.attributes,
            "ai_fix_applied": draft.overrides,
            "use_defaults": draft.use_defaults,
            "settings": {name: getattr(settings, name) for name in ListingDraft.SETTING_FIELDS},
        },
        "product_sku": draft.listed().sku,
        "user_product_seller": uses_user_products(organization),
        "page": (request.headers.get("Referer") or "")[:500],
    }
    row, created = report(
        organization=organization,
        user=request.user,
        source=ErrorReport.Source.LISTING_DRAFT,
        object_id=draft.pk,
        title=draft.error,
        details=draft.error_details,
        context=context,
        note=(request.POST.get("note") or "").strip(),
    )
    return render(request, "support/_reported.html", {"report": row, "created": created})
