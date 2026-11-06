import logging

from django.conf import settings
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render

from accounts.access import require_app
from accounts.models import AppAccess, can_edit_products, membership_for
from catalog.forms import ImportForm, attribute_rows, attributes_from
from catalog.models import Brand
from catalog.views import _KINDS, _code_dialog

from .client import MercadoLibreError, describe_failure
from .drafts import DraftError
from .importer import ImportProblem, read
from .models import MercadoLibreAccount

logger = logging.getLogger(__name__)
READ_ONLY = "Only owners and admins can import products."


@require_app(AppAccess.App.CATALOG)
def catalog_import(request):
    """Paste a Mercado Libre link, review what the API says about the product, and save it to the catalog."""
    membership = membership_for(request.user)
    organization = membership.organization
    if not can_edit_products(membership):
        if request.method == "POST":
            return HttpResponseForbidden(READ_ONLY)
        return render(request, "meli/import.html", {"can_edit": False, "active_tab": "products"})
    connected = MercadoLibreAccount.objects.filter(organization=organization).exists()
    context = {"can_edit": True, "connected": connected, "active_tab": "products", "url": "", "error": ""}
    action = request.POST.get("action") if request.method == "POST" else ""
    # Create listing → From a catalog link sends a link that isn't in Vimple yet: read it straight away.
    if request.method == "GET" and request.GET.get("url"):
        action = "read"
    if action == "read" and connected:
        url = (request.POST.get("url") or request.GET.get("url") or "").strip()
        context["url"] = url
        try:
            values = read(organization, url)
        except (ImportProblem, DraftError) as error:
            context["error"] = str(error)
        except MercadoLibreError as error:
            context["error"] = f"Mercado Libre said: {error.message}"
        except Exception as error:
            logger.warning("Import of %s failed: %s", url[:200], describe_failure(error))
            context["error"] = "Mercado Libre didn't answer. Try again."
        else:
            initial = {key: values[key] for key in ("title", "brand_hint", "variant", "gtin", "description",
                                                    "meli_catalog_product_id", "meli_category_id")}
            initial["url"] = url
            brand = Brand.objects.filter(organization=organization, name__iexact=values["brand_hint"]).first()
            if values["brand_hint"] and brand is not None:
                initial["brand"] = brand.pk
            form = ImportForm(initial=initial, organization=organization)
            return _review(request, organization, form, values["pictures"], values["pictures"], values["attributes"])
    elif action == "save":
        attributes = attributes_from(request.POST)
        options = [url for url in request.POST.getlist("picture_option") if url][:10]
        chosen = [url for url in options if url in request.POST.getlist("picture")]
        form = ImportForm(request.POST, organization=organization, pictures=chosen, attributes=attributes)
        if form.is_valid():
            product = form.save()
            return redirect("catalog_product", pk=product.pk)
        return _review(request, organization, form, options, chosen, attributes)
    return render(request, "meli/import.html", context)


def _review(request, organization, form, options, chosen, attributes):
    return render(
        request,
        "meli/import.html",
        {
            "form": form,
            "pictures": [(url, url in chosen) for url in options],
            "attributes": attribute_rows(attributes),
            "code_dialogs": [_code_dialog(organization, kind) for kind in _KINDS],
            "ai": bool(settings.MISTRAL_API_KEY),
            "can_edit": True,
            "connected": True,
            "active_tab": "products",
        },
    )
