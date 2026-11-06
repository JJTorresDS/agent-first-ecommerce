"""Mercado Libre listings prepared in Vimple: drafts, the item body, the check and the post.

A draft is checked with POST /items/validate before anyone can post it, so a value Mercado Libre
refuses shows its own reason on the draft instead of a broken listing. Posting runs in the worker,
one draft at a time, and re-checks the stock first. Every call is made by the worker, never a page.
"""

import logging
import re
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .client import (
    MercadoLibreError,
    create_item,
    describe_failure,
    fetch_catalog_product,
    fetch_category_attributes,
    only_warnings,
    post_item_description,
    predict_category,
    remove_flex,
    validate_item,
)
from .models import ListingDefaults, ListingDraft, ListingSettings

logger = logging.getLogger(__name__)

# What Mercado Libre's sell flow sends, read from listings made there (MeliListing.payload).
LISTING_TYPE = {False: "gold_special", True: "gold_pro"}
INVOICE = {False: "6891888", True: "6891885"}  # No factura, Factura A
WARRANTY_TYPE = {
    ListingSettings.Warranty.SELLER: "2230280",
    ListingSettings.Warranty.FACTORY: "2230279",
}
CONDITION_NEW = "2230284"
# Mercado Libre requires free shipping from this price (ARS); below it, saying no avoids its
# "free shipping costs exceed the sale" warning. The vimple-publisher scripts use the same line.
FREE_SHIPPING_FROM = Decimal("33000")
REGULATORY = "155_98_RESOLUTION_FILE_NUMBER"
NOT_APPLICABLE = "-1"
CATALOG_URL = re.compile(r"/p/(MLA\d+)", re.IGNORECASE)
LISTING_URL = re.compile(r"\bMLA-?\d+", re.IGNORECASE)


class DraftError(ValueError):
    """A reason to show next to the URL field."""


def catalog_product_id(url):
    """MLA12345678 from a catalog product page URL, or DraftError with what to paste instead."""
    found = CATALOG_URL.search(url or "")
    if found:
        return found.group(1).upper()
    if LISTING_URL.search(url or ""):
        raise DraftError(
            "That's a listing, not a catalog product. Open the catalog product page (Vender uno igual) and paste its URL."
        )
    raise DraftError("Paste a Mercado Libre catalog product URL, like https://www.mercadolibre.com.ar/…/p/MLA12345678.")


def defaults_for(organization):
    defaults, _ = ListingDefaults.objects.get_or_create(organization=organization)
    return defaults


def settings_of(draft, defaults):
    """The settings this draft is posted with: the organization's defaults, or its own."""
    return defaults if draft.use_defaults else draft


def stock_by_product(organization):
    """{product_id: units in the Depósito} for the whole organization, in one query."""
    from inventory.ledger import OWN, on_hand_by_product

    return {product_id: max(0, totals.get(OWN, 0)) for product_id, totals in on_hand_by_product(organization).items()}


def inventory_started(organization):
    from inventory.models import InventorySettings

    return InventorySettings.objects.filter(organization=organization, started_at__isnull=False).exists()


def _number(value):
    value = Decimal(value)
    return int(value) if value == value.to_integral_value() else float(value)


def build_item(draft, settings, user_products=False):
    """The POST /items body for a draft, as Mercado Libre's own sell flow fills it in.

    user_products: the seller has Mercado Libre's user_product_seller tag (uses_user_products).
    """
    sale_terms = [{"id": "INVOICE", "value_id": INVOICE[bool(settings.invoice_a)]}]
    if settings.warranty == ListingSettings.Warranty.NONE:
        sale_terms.append({"id": "WARRANTY_TYPE", "value_name": "Sin garantía"})
    else:
        sale_terms.append({"id": "WARRANTY_TYPE", "value_id": WARRANTY_TYPE[settings.warranty]})
        sale_terms.append({"id": "WARRANTY_TIME", "value_name": f"{settings.warranty_days} días"})
    attributes = [{"id": "SELLER_SKU", "value_name": draft.product.sku}]
    if settings.condition == ListingSettings.Condition.NEW:
        attributes.append({"id": "ITEM_CONDITION", "value_id": CONDITION_NEW})
    else:
        attributes.append({"id": "ITEM_CONDITION", "value_name": "Usado"})
    if draft.has_regulatory and settings.regulatory_na:
        attributes.append({"id": REGULATORY, "value_id": NOT_APPLICABLE})
    body = {
        "price": _number(draft.price),
        "currency_id": "ARS",
        "available_quantity": draft.quantity,
        "buying_mode": "buy_it_now",
        "listing_type_id": LISTING_TYPE[bool(settings.installments)],
        "condition": settings.condition,
        "sale_terms": sale_terms,
        "shipping": {"mode": "me2", "local_pick_up": False, "free_shipping": Decimal(draft.price) >= FREE_SHIPPING_FROM},
        "attributes": attributes,
    }
    attributes.extend(package_attributes(draft.product))
    if draft.kind == ListingDraft.Kind.CATALOG:
        body["catalog_product_id"] = draft.catalog_product_id
        body["catalog_listing"] = True
        # Mercado Libre still requires a title: the catalog product's own name, as its sell flow uses.
        body["title"] = draft.catalog_name
    else:
        product = draft.product
        # A user-products seller sends family_name and Mercado Libre builds the title;
        # any other seller sends title, and family_name is refused as invalid.
        if user_products:
            body["family_name"] = product.title
        else:
            body["title"] = product.title
        body["pictures"] = [{"source": url} for url in pictures_of(product)]
        attributes.extend(product_attributes(draft))
    if draft.category_id:
        body["category_id"] = draft.category_id
    # One value per attribute: the draft's own, added last, wins.
    body["attributes"] = list({attribute["id"]: attribute for attribute in attributes}.values())
    return body


# Mercado Libre's seller package attributes, from the product's Package fields.
PACKAGE = (
    ("SELLER_PACKAGE_HEIGHT", "height_cm", "cm"),
    ("SELLER_PACKAGE_WIDTH", "width_cm", "cm"),
    ("SELLER_PACKAGE_LENGTH", "length_cm", "cm"),
    ("SELLER_PACKAGE_WEIGHT", "weight_g", "g"),
)


def package_attributes(product):
    """"17 cm", "310 g": the formats Mercado Libre's own listings use. Fields left empty are skipped."""
    attributes = []
    for attribute_id, field, unit in PACKAGE:
        value = getattr(product, field)
        if value is not None:
            attributes.append({"id": attribute_id, "value_name": f"{_number(value)} {unit}"})
    return attributes


def package_missing(product):
    return any(getattr(product, field) is None for _, field, _ in PACKAGE)


def pictures_of(product):
    urls = list(product.images.filter(organization_id=product.organization_id).values_list("url", flat=True))
    return urls or [product.image_url]


# Sent from their own fields, or never copied from a product's attributes.
OWN_ATTRIBUTES = {"SELLER_SKU", "ITEM_CONDITION", REGULATORY, *(attribute_id for attribute_id, _, _ in PACKAGE)}


def _text(value):
    if isinstance(value, list):
        return ", ".join(_text(part) for part in value)
    if isinstance(value, bool):
        return "Sí" if value else "No"
    return str(value).strip()


def product_attributes(draft):
    """Brand, model, GTIN, then the product's attributes the category knows, then the draft's own.

    Keys are Mercado Libre ids; a product attribute the category doesn't have is not sent.
    """
    product = draft.product
    model = product.product_model
    known = {attribute["id"] for attribute in draft.category_schema or []}
    values = {"BRAND": model.brand.name, "MODEL": model.name}
    if product.gtin:
        values["GTIN"] = product.gtin
    elif "GTIN" in known:
        # No barcode: Mercado Libre needs the reason, and "Otra razón" fits every case Vimple has.
        values["EMPTY_GTIN_REASON"] = NO_GTIN_REASON
    for key, value in (product.attributes or {}).items():
        if key in known and key not in OWN_ATTRIBUTES and _text(value):
            values[key] = _text(value)
    for key, value in (draft.attributes or {}).items():
        if key not in OWN_ATTRIBUTES and _text(value):
            values[key] = _text(value)
    schema = {attribute["id"]: attribute for attribute in draft.category_schema or []}
    attributes = []
    for key, value in values.items():
        value_id = _allowed_id(schema.get(key), value)
        # A value from Mercado Libre's own list goes by its id, which is what it checks.
        attributes.append({"id": key, "value_id": value_id} if value_id else {"id": key, "value_name": value})
    return attributes


NO_GTIN_REASON = "Otra razón"

# Attribute types whose values must come from Mercado Libre's list; others only suggest values.
FIXED_TYPES = {"list", "boolean"}


def _allowed_id(attribute, value):
    """Mercado Libre's id for this value when the attribute has a list and the value is on it."""
    if not attribute:
        return ""
    for name, value_id in zip(attribute.get("values") or [], attribute.get("value_ids") or []):
        if name.lower() == value.lower() and value_id:
            return value_id
    return ""


def invalid_values(draft, body):
    """Attributes whose value isn't on Mercado Libre's fixed list: [{id, name, values, value}]."""
    schema = {attribute["id"]: attribute for attribute in draft.category_schema or []}
    wrong = []
    for attribute in body.get("attributes") or []:
        known = schema.get(attribute["id"])
        if not known or known.get("type") not in FIXED_TYPES or not known.get("values") or "value_name" not in attribute:
            continue
        if attribute["id"] in OWN_ATTRIBUTES:
            continue
        wrong.append({**known, "value": attribute["value_name"]})
    return wrong


def invalid_message(items):
    return "; ".join(
        f"{item['name']} ({item['id']}): “{item['value']}” isn't one of {', '.join(item['values'][:12])}"
        for item in items
    ) + "."


def missing_attributes(draft, body):
    """The category's required attributes the body doesn't have: [{id, name, values}].

    GTIN is only conditionally required, and product_attributes always sends it or its reason.
    """
    schema = draft.category_schema or []
    sent = {attribute["id"] for attribute in body.get("attributes") or []}
    missing = [
        attribute
        for attribute in schema
        if attribute.get("required") and attribute["id"] not in sent and attribute["id"] not in OWN_ATTRIBUTES
    ]
    return missing


def schema_of(attributes):
    """The parts of /categories/{id}/attributes a draft needs: id, name, required, allowed values."""
    schema = []
    for attribute in attributes or []:
        if not isinstance(attribute, dict) or not attribute.get("id"):
            continue
        tags = attribute.get("tags") or {}
        if isinstance(tags, list):
            tags = {tag: True for tag in tags}
        if tags.get("read_only") or tags.get("hidden"):
            continue
        required = bool(tags.get("required"))
        listed = [value for value in attribute.get("values") or [] if isinstance(value, dict) and value.get("name")]
        kind = str(attribute.get("value_type") or "")
        keep = required or kind in FIXED_TYPES or attribute["id"] == "EMPTY_GTIN_REASON"
        listed = listed[:100] if keep else []
        schema.append(
            {
                "id": str(attribute["id"])[:60],
                "name": str(attribute.get("name") or attribute["id"])[:100],
                "required": required,
                "type": kind,
                "values": [str(value["name"]) for value in listed],
                "value_ids": [str(value.get("id") or "") for value in listed],
            }
        )
    return schema


def uses_user_products(organization):
    """Whether the seller has Mercado Libre's user_product_seller tag, from the stored /users/me answer."""
    from .models import SellerProfile

    profile = SellerProfile.objects.filter(organization=organization).only("payload").first()
    tags = (profile.payload or {}).get("tags") if profile else None
    return isinstance(tags, list) and "user_product_seller" in tags


def _site(organization):
    from .models import SellerProfile

    profile = SellerProfile.objects.filter(organization=organization).only("site_id").first()
    return (profile.site_id if profile else "") or "MLA"


def _token(organization):
    from .postsale import _account_for

    account = _account_for(organization)
    if account is None:
        raise DraftError("Connect Mercado Libre first.")
    return account.fresh_access_token()


def _set(draft, status, error="", details=None):
    draft.status = status
    draft.error = (error or "")[:500]
    draft.error_details = details or {}
    draft.save(update_fields=["status", "error", "error_details", "updated_at"])


def _details(call, body, error):
    """What to show under a refused draft: the call, Mercado Libre's whole answer, and what was sent."""
    if not isinstance(error, MercadoLibreError):
        return {}
    return {"call": call, "status": error.status, "response": error.payload, "sent": body}


def _warnings(error):
    return "; ".join(str(cause.get("message")) for cause in error.payload["cause"] if cause.get("message"))


def _log_refusal(what, draft, error):
    """One log line with Mercado Libre's error and cause codes, never a token."""
    payload = getattr(error, "payload", None)
    codes = ""
    if isinstance(payload, dict):
        causes = [cause.get("code") for cause in payload.get("cause") or [] if isinstance(cause, dict)]
        codes = f" (error={payload.get('error')}, causes={causes})"
    logger.warning("Draft %s %s: %s%s", draft.pk, what, describe_failure(error), codes)


def _stock_problem(draft, stock):
    available = stock.get(draft.product_id, 0)
    if available < 1:
        return "No stock left in the Depósito."
    if draft.quantity > available:
        return f"Only {available} in stock in the Depósito; lower the quantity."
    return ""


def check(organization, draft_ids):
    """Read the catalog product, find the category, and let Mercado Libre validate each draft."""
    drafts = list(
        ListingDraft.objects.filter(organization=organization, pk__in=draft_ids, status=ListingDraft.Status.CHECKING)
        .select_related("product__product_model__brand")
    )
    if not drafts:
        return
    try:
        token = _token(organization)
    except Exception as error:
        logger.warning("Draft check failed for %s: %s", organization.pk, describe_failure(error))
        for draft in drafts:
            _set(draft, ListingDraft.Status.NEEDS_FIX, _reason(error))
        return
    defaults = defaults_for(organization)
    stock = stock_by_product(organization)
    site = _site(organization)
    user_products = uses_user_products(organization)
    for draft in drafts:
        try:
            _check_one(draft, token, defaults, stock, site, user_products)
        except Exception as error:
            _log_refusal("check failed", draft, error)
            _set(draft, ListingDraft.Status.NEEDS_FIX, _reason(error), getattr(error, "details", None))


def _check_one(draft, token, defaults, stock, site, user_products=False):
    if draft.kind == ListingDraft.Kind.CATALOG:
        product = fetch_catalog_product(token, draft.catalog_product_id)
        if product.get("status") not in (None, "active"):
            _set(draft, ListingDraft.Status.NEEDS_FIX, "This catalog product isn't active on Mercado Libre.")
            return
        pictures = product.get("pictures") or []
        draft.catalog_name = str(product.get("name") or "")[:255]
        draft.catalog_picture = str((pictures[0] or {}).get("url") or "")[:1024] if pictures else ""
        if not draft.category_id:
            draft.category_id = _category_for(token, site, product)
    elif not draft.category_id:
        draft.category_id = draft.product.meli_category_id or _category_for(
            token, site, {"name": draft.product.title}
        )
    if draft.category_id:
        attributes = fetch_category_attributes(token, draft.category_id)
        draft.has_regulatory = any(attribute.get("id") == REGULATORY for attribute in attributes)
        draft.category_schema = schema_of(attributes)
    draft.save(
        update_fields=["catalog_name", "catalog_picture", "category_id", "has_regulatory", "category_schema", "updated_at"]
    )
    problem = _stock_problem(draft, stock)
    if problem:
        _set(draft, ListingDraft.Status.NEEDS_FIX, problem)
        return
    if draft.kind != ListingDraft.Kind.CATALOG and not draft.category_id:
        _set(draft, ListingDraft.Status.NEEDS_FIX, "Mercado Libre didn't suggest a category: enter one (MLA…).")
        return
    if draft.kind != ListingDraft.Kind.CATALOG and package_missing(draft.product):
        _set(
            draft,
            ListingDraft.Status.NEEDS_FIX,
            "Mercado Libre needs the package weight and size: fill in Package on the product, then Save and check.",
        )
        return
    body = build_item(draft, settings_of(draft, defaults), user_products)
    missing = missing_attributes(draft, body) if draft.kind != ListingDraft.Kind.CATALOG else []
    if missing:
        names = ", ".join(attribute["name"] for attribute in missing)
        _set(draft, ListingDraft.Status.NEEDS_FIX, f"Missing: {names}. Add them below.")
        return
    wrong = invalid_values(draft, body) if draft.kind != ListingDraft.Kind.CATALOG else []
    if wrong:
        _set(draft, ListingDraft.Status.NEEDS_FIX, invalid_message(wrong))
        return
    try:
        validate_item(token, body)
    except MercadoLibreError as error:
        details = _details("POST /items/validate", body, error)
        if only_warnings(error):
            # Warnings don't block a listing: it's ready, and they stay visible.
            _set(draft, ListingDraft.Status.READY, f"Mercado Libre warns: {_warnings(error)}", details)
            return
        error.details = details
        raise
    _set(draft, ListingDraft.Status.READY)


def _category_for(token, site, product):
    """The catalog product's own category, else Mercado Libre's guess for its name in the same domain."""
    if product.get("category_id"):
        return str(product["category_id"])
    guesses = predict_category(token, site, product.get("name") or "")
    domain = product.get("domain_id")
    for guess in guesses:
        if isinstance(guess, dict) and guess.get("domain_id") == domain and guess.get("category_id"):
            return str(guess["category_id"])
    for guess in guesses:
        if isinstance(guess, dict) and guess.get("category_id"):
            return str(guess["category_id"])
    return ""


def queue_for_posting(organization, draft_ids):
    """Ready drafts among these become queued. Returns the ids that did; a repeated click queues nothing twice."""
    with transaction.atomic():
        ids = list(
            ListingDraft.objects.select_for_update()
            .filter(organization=organization, pk__in=draft_ids, status=ListingDraft.Status.READY)
            .values_list("pk", flat=True)
        )
        ListingDraft.objects.filter(pk__in=ids).update(
            status=ListingDraft.Status.QUEUED, error="", error_details={}, updated_at=timezone.now()
        )
    return ids


def publish(organization, draft_ids):
    """Post each queued draft to Mercado Libre. One failure doesn't stop the others."""
    from .listings import refresh_listing

    try:
        token = _token(organization)
    except Exception as error:
        logger.warning("Posting failed for %s: %s", organization.pk, describe_failure(error))
        ListingDraft.objects.filter(
            organization=organization, pk__in=draft_ids, status=ListingDraft.Status.QUEUED
        ).update(status=ListingDraft.Status.FAILED, error=_reason(error)[:500], updated_at=timezone.now())
        return
    defaults = defaults_for(organization)
    site = _site(organization)
    user_products = uses_user_products(organization)
    for draft_id in draft_ids:
        # Claim it: only a queued draft is posted, so a second job for the same one does nothing.
        claimed = ListingDraft.objects.filter(
            organization=organization, pk=draft_id, status=ListingDraft.Status.QUEUED
        ).update(status=ListingDraft.Status.PUBLISHING, updated_at=timezone.now())
        if not claimed:
            continue
        draft = ListingDraft.objects.select_related("product__product_model__brand").get(pk=draft_id)
        problem = _stock_problem(draft, stock_by_product(organization))
        if problem:
            _set(draft, ListingDraft.Status.FAILED, problem)
            continue
        settings = settings_of(draft, defaults)
        body = build_item(draft, settings, user_products)
        try:
            item = create_item(token, body)
        except Exception as error:
            _log_refusal("could not be posted", draft, error)
            _set(draft, ListingDraft.Status.FAILED, _reason(error), _details("POST /items", body, error))
            continue
        draft.item_id = str(item.get("id") or "")[:32]
        draft.published_at = timezone.now()
        draft.status = ListingDraft.Status.PUBLISHED
        draft.error = ""
        draft.error_details = {}
        draft.save(update_fields=["item_id", "published_at", "status", "error", "error_details", "updated_at"])
        logger.info("Draft %s posted as %s", draft.pk, draft.item_id)
        notes = []
        description = draft.product.product_model.description.strip()
        if draft.kind != ListingDraft.Kind.CATALOG and description:
            try:
                post_item_description(token, draft.item_id, description)
            except Exception as error:
                logger.warning("Description of %s wasn't posted: %s", draft.item_id, describe_failure(error))
                notes.append(f"Posted without its description: {_reason(error)}")
        tags = ((item.get("shipping") or {}).get("tags")) or []
        if not settings.flex and "self_service_in" in tags:
            try:
                remove_flex(token, site, draft.item_id)
            except Exception as error:
                logger.warning("Flex stayed on for %s: %s", draft.item_id, describe_failure(error))
                notes.append(f"Posted, but Flex is still on: {_reason(error)}")
        try:
            refresh_listing(organization, draft.item_id)
        except Exception as error:
            logger.warning("Listing %s was posted but not read back: %s", draft.item_id, describe_failure(error))
        if notes:
            draft.error = " ".join(notes)[:500]
            draft.save(update_fields=["error", "updated_at"])


def _reason(error):
    if isinstance(error, (MercadoLibreError, DraftError)):
        return str(error.message if isinstance(error, MercadoLibreError) else error)
    if error.__class__.__name__ == "InvalidToken":
        return "This server can't read the saved Mercado Libre token. Connect Mercado Libre again."
    if isinstance(error, OSError):
        return "Mercado Libre didn't answer. Try again."
    return "Something went wrong. Try again."
