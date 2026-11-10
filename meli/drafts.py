"""Mercado Libre listings prepared in Vimple: drafts, the item body, the check and the post.

A draft is checked with POST /items/validate before anyone can post it, so a value Mercado Libre
refuses shows its own reason on the draft instead of a broken listing. Posting runs in the worker,
one draft at a time, and re-checks the stock first. Every call is made by the worker, never a page.
"""

import json
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


def build_item(draft, settings, user_products=False, overrides=True):
    """The POST /items body for a draft, as Mercado Libre's own sell flow fills it in.

    user_products: the seller has Mercado Libre's user_product_seller tag (uses_user_products).
    overrides: merge the draft's applied AI fix (apply_overrides); False gives the body Vimple makes alone.
    """
    sale_terms = [{"id": "INVOICE", "value_id": INVOICE[bool(settings.invoice_a)]}]
    if settings.warranty == ListingSettings.Warranty.NONE:
        sale_terms.append({"id": "WARRANTY_TYPE", "value_name": "Sin garantía"})
    else:
        sale_terms.append({"id": "WARRANTY_TYPE", "value_id": WARRANTY_TYPE[settings.warranty]})
        sale_terms.append({"id": "WARRANTY_TIME", "value_name": f"{settings.warranty_days} días"})
    listed = draft.listed()
    attributes = [{"id": "SELLER_SKU", "value_name": listed.sku}]
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
    if draft.kind == ListingDraft.Kind.CATALOG:
        # Mercado Libre requires all four and refuses ones smaller than the catalog product's
        # (item.attribute.invalid.seller.package.dimensions): its own measures win over the product's.
        attributes.extend(package_attributes(listed))
        attributes.extend({"id": key, "value_name": value} for key, value in (draft.catalog_package or {}).items())
        body["catalog_product_id"] = draft.catalog_product_id
        body["catalog_listing"] = True
        # A seller without user products must send a title: the catalog product's own name, as its sell flow uses.
        # A user-products seller's title comes from the catalog product, and a title sent is refused
        # ("The fields [title] are invalid for requested call").
        if not user_products:
            body["title"] = draft.catalog_name
    else:
        # A user-products seller sends family_name and Mercado Libre builds the title;
        # any other seller sends title, and family_name is refused as invalid.
        if user_products:
            body["family_name"] = listed.title
        else:
            body["title"] = listed.title
        body["pictures"] = [{"source": url} for url in pictures_of(listed)]
        attributes.extend(package_attributes(listed))
        attributes.extend(product_attributes(draft))
    if draft.category_id:
        body["category_id"] = draft.category_id
    # One value per attribute: the draft's own, added last, wins.
    body["attributes"] = list({attribute["id"]: attribute for attribute in attributes}.values())
    return apply_overrides(draft, body) if overrides else body


# Fields an AI fix never changes: Vimple owns them, and stock, the sync and the post depend on them.
LOCKED_KEYS = frozenset(
    {
        "price",
        "available_quantity",
        "currency_id",
        "category_id",
        "catalog_product_id",
        "catalog_listing",
        "listing_type_id",
        "buying_mode",
        "pictures",
    }
)
LOCKED_ATTRIBUTES = frozenset({"SELLER_SKU"})
ATTRIBUTE_VALUE_KEYS = ("value_id", "value_name", "value_struct", "values")
FIX_LIMIT = 20000


class FixRefused(ValueError):
    """What to show next to the AI fix's JSON."""


def _attribute_value(attribute):
    return {key: attribute[key] for key in ATTRIBUTE_VALUE_KEYS if attribute.get(key) not in (None, "", [])}


def apply_overrides(draft, body):
    """The body with the draft's applied AI fix merged in: keys replaced or removed (null), attributes by id.

    The locked fields and SELLER_SKU always stay as build_item made them.
    """
    overrides = draft.overrides if isinstance(draft.overrides, dict) else {}
    if not overrides:
        return body
    for key, value in overrides.items():
        if key in LOCKED_KEYS or key == "attributes":
            continue
        if value is None:
            body.pop(key, None)
        else:
            body[key] = value
    by_id = {attribute["id"]: attribute for attribute in body.get("attributes") or []}
    for attribute_id, value in (overrides.get("attributes") or {}).items():
        if attribute_id in LOCKED_ATTRIBUTES:
            continue
        if value is None:
            by_id.pop(attribute_id, None)
        elif isinstance(value, dict):
            by_id[attribute_id] = {"id": attribute_id, **value}
    body["attributes"] = list(by_id.values())
    return body


def clean_fix(edited, base):
    """The overrides an edited body makes over build_item's own (base): only what differs and may change.

    Raises FixRefused when it isn't a listing body.
    """
    if not isinstance(edited, dict):
        raise FixRefused("The fix must be a JSON object, like the body under Details.")
    overrides = {}
    for key in sorted(set(edited) | set(base)):
        if key in LOCKED_KEYS or key == "attributes":
            continue
        if not re.fullmatch(r"[a-z_]{1,40}", key):
            raise FixRefused(f"{key} isn't a Mercado Libre listing field.")
        if key not in edited:
            overrides[key] = None
        elif edited[key] != base.get(key):
            overrides[key] = edited[key]
    attributes = edited.get("attributes", base.get("attributes") or [])
    if not isinstance(attributes, list):
        raise FixRefused("attributes must be a list of {id, value_name} or {id, value_id}.")
    before = {attribute["id"]: _attribute_value(attribute) for attribute in base.get("attributes") or []}
    after = {}
    for attribute in attributes:
        if not isinstance(attribute, dict) or not re.fullmatch(r"[A-Z0-9_]{1,60}", str(attribute.get("id") or "")):
            raise FixRefused("Each attribute needs an id such as BRAND and a value_name or value_id.")
        value = _attribute_value(attribute)
        if not value:
            raise FixRefused(f"{attribute['id']} has no value: remove it, or give it a value_name or value_id.")
        after[attribute["id"]] = value
    changed = {}
    for attribute_id in sorted(set(before) | set(after)):
        if attribute_id in LOCKED_ATTRIBUTES:
            continue
        if attribute_id not in after:
            changed[attribute_id] = None
        elif after[attribute_id] != before.get(attribute_id):
            changed[attribute_id] = after[attribute_id]
    if changed:
        overrides["attributes"] = changed
    if len(json.dumps(overrides, ensure_ascii=False, default=str)) > FIX_LIMIT:
        raise FixRefused("The fix is too long to keep.")
    return overrides


def _shown(value):
    if isinstance(value, dict) and set(value) <= set(ATTRIBUTE_VALUE_KEYS) | {"id"}:
        value = value.get("value_name") or value.get("value_id") or value.get("value_struct") or value.get("values")
    if value is None or value == "":
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


def fix_changes(sent, edited, notes=()):
    """[{field, before, after, why, estimated}]: what an AI fix changes in the body sent, with the AI's reasons."""
    by_field = {str(note.get("field") or ""): note for note in notes if isinstance(note, dict)}
    changes = []
    for key, value in clean_fix(edited, sent).items():
        pairs = (
            [(attribute_id, next((a for a in sent.get("attributes") or [] if a.get("id") == attribute_id), None), after)
             for attribute_id, after in value.items()]
            if key == "attributes"
            else [(key, sent.get(key), value)]
        )
        for field, before, after in pairs:
            note = by_field.get(field) or by_field.get(field.lower()) or {}
            changes.append(
                {
                    "field": field,
                    "before": _shown(before)[:300],
                    "after": _shown(after)[:300],
                    "why": str(note.get("why") or "")[:300],
                    "estimated": bool(note.get("estimated")),
                }
            )
    return changes


def package_from_fix(overrides):
    """{model field: value} for the SELLER_PACKAGE_ attributes a fix sets, and the ones that can't be read."""
    values, unread = {}, []
    for attribute_id, field, unit in PACKAGE:
        value = ((overrides.get("attributes") or {}).get(attribute_id) or {}).get("value_name")
        if value is None:
            continue
        found = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*(mm|cm|m|g|kg)\s*", str(value), re.IGNORECASE)
        if not found:
            unread.append(attribute_id)
            continue
        number, given = Decimal(found.group(1).replace(",", ".")), found.group(2).lower()
        scale = {"mm": Decimal("0.1"), "cm": 1, "m": 100, "g": 1, "kg": 1000}[given]
        if (unit == "g") != (given in {"g", "kg"}):
            unread.append(attribute_id)
            continue
        number *= scale
        values[field] = int(number.to_integral_value()) if unit == "g" else number.quantize(Decimal("0.1"))
    return values, unread


# Mercado Libre's seller package attributes, from the product's Package fields.
PACKAGE = (
    ("SELLER_PACKAGE_HEIGHT", "height_cm", "cm"),
    ("SELLER_PACKAGE_WIDTH", "width_cm", "cm"),
    ("SELLER_PACKAGE_LENGTH", "length_cm", "cm"),
    ("SELLER_PACKAGE_WEIGHT", "weight_g", "g"),
)


def catalog_package(product):
    """A catalog product's PACKAGE_HEIGHT/WIDTH/LENGTH/WEIGHT as SELLER_PACKAGE_ values ("12 cm")."""
    found = {}
    for attribute in product.get("attributes") or []:
        name = f"SELLER_{attribute.get('id')}"
        if name not in {attribute_id for attribute_id, _, _ in PACKAGE}:
            continue
        value = str(attribute.get("value_name") or "").strip()
        struct = attribute.get("value_struct") or {}
        if not value and struct.get("number") is not None and struct.get("unit"):
            value = f"{_number(str(struct['number']))} {struct['unit']}"
        if value:
            found[name] = value[:30]
    return found


def package_attributes(product):
    """"17 cm", "310 g": the formats Mercado Libre's own listings use. Fields left empty are skipped."""
    attributes = []
    for attribute_id, field, unit in PACKAGE:
        value = getattr(product, field)
        if value is not None:
            attributes.append({"id": attribute_id, "value_name": f"{_number(value)} {unit}"})
    return attributes


def listed_as_generic(draft):
    """A regular draft whose product has no GTIN goes out as brand Generic, unless the draft names a brand."""
    known = {attribute["id"] for attribute in draft.category_schema or []}
    return (
        draft.regular()
        and not draft.listed().gtin
        and "GTIN" in known
        and not (draft.attributes or {}).get("BRAND")
    )


def package_missing(listed):
    from catalog.models import package_missing as missing

    return bool(missing(listed))


def pictures_of(listed):
    """A product's pictures in order; a combo has its one picture."""
    images = getattr(listed, "images", None)
    urls = list(images.filter(organization_id=listed.organization_id).values_list("url", flat=True)) if images else []
    return urls or [listed.image_url]


def description_of(listed):
    """The text posted after a regular listing: the combo's own, or the product's model description."""
    model = getattr(listed, "product_model", None)
    return ((model.description if model else listed.description) or "").strip()


def brand_and_model(listed):
    """A product's brand and model names; a combo has no brand of its own, so Generic and its title."""
    model = getattr(listed, "product_model", None)
    if model is None:
        return GENERIC_BRAND, listed.title[:255]
    return model.brand.name, model.name


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
    product = draft.listed()
    brand, model = brand_and_model(product)
    known = {attribute["id"] for attribute in draft.category_schema or []}
    values = {"BRAND": brand, "MODEL": model}
    if product.gtin:
        values["GTIN"] = product.gtin
    elif "GTIN" in known:
        # No barcode: a registered brand (Onlyou) is refused even with a reason, so the listing
        # goes out as brand Generic, as Mercado Libre accepts it. The draft says so.
        values["BRAND"] = GENERIC_BRAND
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
        if not value_id and key == "EMPTY_GTIN_REASON" and value.lower() == NO_GTIN_REASON.lower():
            # Sent as text, Mercado Libre still asks for the GTIN; its listings carry this id.
            value_id = NO_GTIN_REASON_ID
        if not value_id and key == "BRAND" and value.lower() == GENERIC_BRAND.lower():
            value_id = GENERIC_BRAND_ID
        # A value from Mercado Libre's own list goes by its id, which is what it checks.
        attributes.append({"id": key, "value_id": value_id} if value_id else {"id": key, "value_name": value})
    return attributes


NUMBER_UNIT = re.compile(r"^\s*\d+([.,]\d+)?\s*[^\d\s].*$")
NO_GTIN_REASON = "Otra razón"
NO_GTIN_REASON_ID = "17055161"
# The brand Mercado Libre accepts without a barcode (id from a perfume listing it took).
GENERIC_BRAND = "Generic"
GENERIC_BRAND_ID = "35977846"

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
        if not known or "value_name" not in attribute or attribute["id"] in OWN_ATTRIBUTES:
            continue
        if known.get("type") == "number_unit" and not NUMBER_UNIT.match(attribute["value_name"]):
            # "ML" or "treinta": Mercado Libre wants "30 mL".
            wrong.append({**known, "values": [], "value": attribute["value_name"]})
        elif known.get("type") in FIXED_TYPES and known.get("values"):
            wrong.append({**known, "value": attribute["value_name"]})
    return wrong


def invalid_message(items):
    return "; ".join(
        f"{item['name']} ({item['id']}): “{item['value']}” "
        + (f"isn't one of {', '.join(item['values'][:12])}" if item.get("values") else "needs a number and a unit, like 30 mL")
        for item in items
    ) + "."


def missing_attributes(draft, body):
    """The category's required attributes the body doesn't have: [{id, name, values}].

    GTIN is only conditionally required: without one, product_attributes sends brand Generic.
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
        if tags.get("read_only"):
            continue
        # Hidden ones (EMPTY_GTIN_REASON) are kept for their values and ids, but never asked for.
        hidden = bool(tags.get("hidden"))
        required = bool(tags.get("required")) and not hidden
        listed = [value for value in attribute.get("values") or [] if isinstance(value, dict) and value.get("name")]
        kind = str(attribute.get("value_type") or "")
        keep = required or kind in FIXED_TYPES or attribute["id"] == "EMPTY_GTIN_REASON"
        listed = listed[:100] if keep else []
        units = [
            str(unit.get("name") or unit.get("id"))
            for unit in attribute.get("allowed_units") or []
            if isinstance(unit, dict) and (unit.get("name") or unit.get("id"))
        ]
        schema.append(
            {
                "id": str(attribute["id"])[:60],
                "name": str(attribute.get("name") or attribute["id"])[:100],
                "required": required,
                "hidden": hidden,
                "type": kind,
                "values": [str(value["name"]) for value in listed],
                "value_ids": [str(value.get("id") or "") for value in listed],
                "units": units[:20],
                "default_unit": str(attribute.get("default_unit") or "")[:20],
                "hint": str(attribute.get("hint") or attribute.get("tooltip") or "")[:300],
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


def available_for(draft, stock):
    """Units this draft can sell: the product's Depósito stock, or how many complete combos it makes."""
    if draft.combo_id:
        return draft.combo.can_make(stock)
    return stock.get(draft.product_id, 0)


def _stock_problem(draft, stock):
    available = available_for(draft, stock)
    if available < 1:
        return "No stock left in the Depósito."
    if draft.quantity > available:
        return f"Only {available} in stock in the Depósito; lower the quantity."
    return ""


def check(organization, draft_ids):
    """Read the catalog product, find the category, and let Mercado Libre validate each draft."""
    drafts = list(
        ListingDraft.objects.filter(organization=organization, pk__in=draft_ids, status=ListingDraft.Status.CHECKING)
        .select_related("product__product_model__brand", "combo")
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
        draft.catalog_package = catalog_package(product)
        # Picked again on every check: a category from another domain is refused, so an earlier wrong
        # guess must not stay. The draft's own stays only when nothing in the domain is found.
        draft.category_id = _category_for(token, site, product) or draft.category_id
    elif not draft.category_id:
        draft.category_id = draft.listed().meli_category_id or _category_for(
            token, site, {"name": draft.listed().title}
        )
    if draft.category_id:
        attributes = fetch_category_attributes(token, draft.category_id)
        draft.has_regulatory = any(attribute.get("id") == REGULATORY for attribute in attributes)
        draft.category_schema = schema_of(attributes)
    draft.save(
        update_fields=["catalog_name", "catalog_picture", "catalog_package", "category_id", "has_regulatory", "category_schema", "updated_at"]
    )
    # A template is posted paused with quantity 1: it never sells, so stock doesn't hold it back.
    problem = "" if draft.template else _stock_problem(draft, stock)
    if problem:
        _set(draft, ListingDraft.Status.NEEDS_FIX, problem)
        return
    if draft.kind != ListingDraft.Kind.CATALOG and not draft.category_id:
        _set(draft, ListingDraft.Status.NEEDS_FIX, "Mercado Libre didn't suggest a category: enter one (MLA…).")
        return
    body = build_item(draft, settings_of(draft, defaults), user_products)
    # All four, from the catalog product, the product's Package or an applied AI fix.
    sent = {attribute["id"] for attribute in body["attributes"]}
    if any(attribute_id not in sent for attribute_id, _, _ in PACKAGE):
        _set(
            draft,
            ListingDraft.Status.NEEDS_FIX,
            "This catalog product has no package measures on Mercado Libre: fill in Package on the product, "
            "then Save and check."
            if draft.kind == ListingDraft.Kind.CATALOG
            else "Mercado Libre needs the package weight and size: fill in Package on the product, then Save and check.",
        )
        return
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
    """The catalog product's own category, else Mercado Libre's guess in the catalog product's domain.

    A guess from another domain is refused (item_domain_different_from_category_domain): "Caramel Dream
    Sweet Tooth" alone is guessed as candy, so the name is asked again with the domain's word ("perfumes").
    """
    if product.get("category_id"):
        return str(product["category_id"])
    name = str(product.get("name") or "")
    domain = str(product.get("domain_id") or "")
    if not domain:
        for guess in predict_category(token, site, name):
            if isinstance(guess, dict) and guess.get("category_id"):
                return str(guess["category_id"])
        return ""
    word = domain.split("-", 1)[-1].replace("_", " ").lower()
    for query in (name, f"{name} {word}"):
        for guess in predict_category(token, site, query):
            if isinstance(guess, dict) and guess.get("domain_id") == domain and guess.get("category_id"):
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
        draft = ListingDraft.objects.select_related("product__product_model__brand", "combo").get(pk=draft_id)
        problem = "" if draft.template else _stock_problem(draft, stock_by_product(organization))
        description = ""
        if draft.kind != ListingDraft.Kind.CATALOG:
            from .description import EMPTY, compose

            # Composed now, from the blocks as they read at posting time; kept as sent.
            description = compose(draft)
            if draft.description_parts and not description:
                problem = problem or EMPTY
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
        draft.ai_fix = {}
        draft.overrides = {}
        draft.save(
            update_fields=[
                "item_id", "published_at", "status", "error", "error_details", "ai_fix", "overrides", "updated_at"
            ]
        )
        logger.info("Draft %s posted as %s", draft.pk, draft.item_id)
        notes = []
        if draft.template:
            from .listing_templates import posted

            # Paused before anything else: a template must never be for sale.
            note = posted(organization, draft, token)
            if note:
                notes.append(note)
        if description:
            draft.posted_description = description
            draft.save(update_fields=["posted_description", "updated_at"])
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


def refused_attributes(draft):
    """Category attributes Mercado Libre's last refusal names (UNIT_VOLUME in "Attribute UNIT_VOLUME with value ML…")."""
    response = (draft.error_details or {}).get("response")
    if not isinstance(response, dict):
        return []
    text = " ".join(
        f"{cause.get('message', '')} {' '.join(map(str, cause.get('references') or []))}"
        for cause in response.get("cause") or []
        if isinstance(cause, dict) and cause.get("type") != "warning"
    ).upper()
    named = set(re.findall(r"\b[A-Z0-9_]{3,}\b", text))
    return [
        attribute
        for attribute in draft.category_schema or []
        if attribute["id"] in named and attribute["id"] not in OWN_ATTRIBUTES and attribute["id"] != "GTIN"
    ]


def attributes_to_fill(draft, body):
    """What Fill with AI is asked for: missing, refused by the list, named in Mercado Libre's error;
    else every required attribute, to suggest them again."""
    current = {attribute["id"]: attribute.get("value_name", "") for attribute in body.get("attributes") or []}
    asked = {}
    for item in missing_attributes(draft, body) + invalid_values(draft, body) + refused_attributes(draft):
        asked.setdefault(item["id"], {**item, "value": item.get("value") or current.get(item["id"], "")})
    if not asked:
        for item in draft.category_schema or []:
            if item.get("required") and item["id"] not in OWN_ATTRIBUTES and item["id"] not in {"BRAND", "MODEL", "GTIN"}:
                asked[item["id"]] = item
    return list(asked.values())
