import logging

from django.conf import settings
from django.db import transaction
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render

from accounts.access import require_app
from accounts.models import AppAccess, can_edit_products, membership_for
from catalog.forms import ImportForm, attribute_rows, attributes_from
from catalog.models import Brand, Category, ProductModel
from catalog.views import _KINDS, _code_dialog

from .client import MercadoLibreError, describe_failure
from .drafts import DraftError
from .importer import ImportProblem, read, read_prospect
from .models import MercadoLibreAccount, MeliListingProspect

logger = logging.getLogger(__name__)
READ_ONLY = "Only owners and admins can import products."
STARTS = ("link", "scraped")


def _unlinked(organization):
    """This organization's scraped listings not linked to a product yet, dismissed ones left out."""
    return MeliListingProspect.objects.filter(organization=organization, product__isnull=True).exclude(
        status=MeliListingProspect.Status.DISMISSED
    )


@require_app(AppAccess.App.CATALOG)
def catalog_import(request):
    """Paste a Mercado Libre link or pick a scraped listing, review what it says about the product, and save it."""
    membership = membership_for(request.user)
    organization = membership.organization
    if not can_edit_products(membership):
        if request.method == "POST":
            return HttpResponseForbidden(READ_ONLY)
        return render(request, "meli/import.html", {"can_edit": False, "active_tab": "products"})
    connected = MercadoLibreAccount.objects.filter(organization=organization).exists()
    start = request.GET.get("start") if request.GET.get("start") in STARTS else "link"
    context = {
        "can_edit": True, "connected": connected, "active_tab": "products", "url": "", "error": "", "start": start,
    }
    action = request.POST.get("action") if request.method == "POST" else ""
    if request.method == "GET" and request.GET.get("prospect"):
        prospect = get_object_or_404(_unlinked(organization), pk=_int(request.GET["prospect"]))
        values, note = read_prospect(organization, prospect)
        return _review_prospect(request, organization, prospect, values, note)
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
            form = ImportForm(initial=_initial(organization, values, url), organization=organization)
            return _review(request, organization, form, values["pictures"], values["pictures"], values["attributes"])
    elif action == "save":
        attributes = attributes_from(request.POST)
        options = [url for url in request.POST.getlist("picture_option") if url][:10]
        chosen = [url for url in options if url in request.POST.getlist("picture")]
        form = ImportForm(request.POST, organization=organization, pictures=chosen, attributes=attributes)
        prospect = _unlinked(organization).filter(pk=_int(request.POST.get("prospect"))).first()
        if form.is_valid():
            with transaction.atomic():
                product = form.save()
                if prospect is not None:
                    prospect.product = product
                    prospect.save(update_fields=["product", "updated_at"])
            return redirect("catalog_product", pk=product.pk)
        return _review(request, organization, form, options, chosen, attributes, prospect=prospect)
    if start == "scraped":
        context["prospects"] = list(_unlinked(organization)[:200])
    return render(request, "meli/import.html", context)


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _initial(organization, values, url):
    initial = {key: values[key] for key in ("title", "brand_hint", "variant", "gtin", "description",
                                            "meli_catalog_product_id", "meli_category_id")}
    initial["url"] = url
    brand = Brand.objects.filter(organization=organization, name__iexact=values["brand_hint"]).first()
    if values["brand_hint"] and brand is not None:
        initial["brand"] = brand.pk
    return initial


def _review_prospect(request, organization, prospect, values, note):
    """The review form for a scraped listing, with the AI's codes applied: an existing category, brand or
    model is selected; a new category or brand waits in its + dialog; a new model fills code and name."""
    initial = _initial(organization, values, prospect.url)
    initial["prospect"] = prospect.pk
    if values.get("list_price") is not None:
        initial["list_price"] = values["list_price"]
    suggested = values.get("suggested") or {}
    dialogs = {}
    notes = [note] if note else []
    picked = {}
    for kind, Model in (("category", Category), ("brand", Brand)):
        code = suggested.get(f"{kind}_code")
        if not code:
            continue
        row = Model.objects.filter(organization=organization, code=code).first()
        if row is not None:
            initial[kind], picked[kind] = row.pk, row
        else:
            name = suggested.get(f"{kind}_name", "")
            dialogs[kind] = {"code": code, "name": name}
            notes.append(f"Suggested new {kind} {code} · {name}: add it with +.")
    brand = picked.get("brand") or Brand.objects.filter(pk=initial.get("brand"), organization=organization).first()
    model_code = suggested.get("model_code")
    model = None
    if model_code and brand is not None:
        model = ProductModel.objects.filter(organization=organization, brand=brand, code=model_code).first()
    if model is not None:
        initial["product_model"] = model.pk
    elif model_code:
        initial["model_code"] = model_code
        initial["model_name"] = suggested.get("model_name", "")
    form = ImportForm(initial=initial, organization=organization)
    return _review(
        request, organization, form, values["pictures"], values["pictures"], values["attributes"],
        prospect=prospect, note=" ".join(notes), dialogs=dialogs,
    )


def _review(request, organization, form, options, chosen, attributes, prospect=None, note="", dialogs=None):
    dialogs = dialogs or {}
    return render(
        request,
        "meli/import.html",
        {
            "form": form,
            "pictures": [(url, url in chosen) for url in options],
            "attributes": attribute_rows(attributes),
            "code_dialogs": [_code_dialog(organization, kind, initial=dialogs.get(kind)) for kind in _KINDS],
            "ai": bool(settings.MISTRAL_API_KEY),
            "can_edit": True,
            "connected": True,
            "active_tab": "products",
            "prospect": prospect,
            "note": note,
        },
    )
