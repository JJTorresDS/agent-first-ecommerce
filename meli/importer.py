"""A Mercado Libre product or listing, read through the API, as values for a new catalog product.

Nothing is saved here: the import page shows the values for the user to review. Reads only;
the token never leaves the server.
"""

import re
from urllib.parse import parse_qs, urlparse

from django.core.exceptions import ValidationError

from catalog.models import MAX_IMAGES
from catalog.validators import normalize_code, validate_gtin, validate_variant

from .client import fetch_catalog_product, fetch_item_description, fetch_items

CATALOG_PRODUCT = re.compile(r"/p/(MLA\d+)", re.IGNORECASE)
LISTING = re.compile(r"\b(MLA)-?(\d{6,})\b", re.IGNORECASE)
# Seller-specific, or kept in their own fields: not product facts to copy.
SKIPPED = {
    "SELLER_SKU",
    "ITEM_CONDITION",
    "VALUE_ADDED_TAX",
    "IMPORT_DUTY",
    "GTIN",
    "155_98_RESOLUTION_FILE_NUMBER",
    "PACKAGE_DATA_SOURCE",
    "SHIPMENT_PACKING",
}
SKIPPED_PREFIXES = ("SELLER_PACKAGE_", "PACKAGE_")


class ImportProblem(ValueError):
    """What to show next to the link field."""


def source_of(url):
    """("product", MLA…) for a catalog page, ("item", MLA…) for a listing, else ImportProblem."""
    url = (url or "").strip()
    found = CATALOG_PRODUCT.search(url)
    if found:
        return "product", found.group(1).upper()
    # /up/MLAU… pages carry the listing in their wid parameter.
    for value in parse_qs(urlparse(url).query).get("wid", []) + parse_qs(urlparse(url).fragment).get("wid", []):
        listing = LISTING.search(value)
        if listing:
            return "item", f"MLA{listing.group(2)}"
    listing = LISTING.search(urlparse(url).path)
    if listing:
        return "item", f"MLA{listing.group(2)}"
    raise ImportProblem("Paste a Mercado Libre product page or listing link, like …/p/MLA46233297 or …/MLA-1234567890-….")


def read(organization, url):
    """The values for a new product from this link. Raises ImportProblem, MercadoLibreError or OSError."""
    from .drafts import _category_for, _site, _token

    kind, source_id = source_of(url)
    token = _token(organization)
    if kind == "product":
        body = fetch_catalog_product(token, source_id)
        if not isinstance(body, dict) or not body.get("name"):
            raise ImportProblem("Mercado Libre didn't return that catalog product.")
        pictures = [picture.get("url") for picture in body.get("pictures") or [] if isinstance(picture, dict)]
        short = body.get("short_description")
        description = str(short.get("content") or "") if isinstance(short, dict) else ""
        return _values(
            name=body.get("name"),
            attributes=body.get("attributes"),
            pictures=pictures,
            description=description,
            catalog_product_id=source_id,
            category_id=_category_for(token, _site(organization), body),
        )
    item = fetch_items(token, [source_id]).get(source_id)
    if not item:
        raise ImportProblem("Mercado Libre doesn't have that listing any more.")
    pictures = [
        picture.get("secure_url") or picture.get("url")
        for picture in item.get("pictures") or []
        if isinstance(picture, dict)
    ]
    return _values(
        name=item.get("title"),
        attributes=item.get("attributes"),
        pictures=pictures,
        description=fetch_item_description(token, source_id),
        catalog_product_id=str(item.get("catalog_product_id") or ""),
        category_id=str(item.get("category_id") or ""),
    )


def _value(attribute):
    if attribute.get("value_name"):
        return str(attribute["value_name"])
    values = attribute.get("values") or []
    if values and isinstance(values[0], dict) and values[0].get("name"):
        return str(values[0]["name"])
    return ""


def _values(*, name, attributes, pictures, description, catalog_product_id, category_id):
    facts = {}
    for attribute in attributes or []:
        if not isinstance(attribute, dict):
            continue
        key = str(attribute.get("id") or "")
        value = _value(attribute)
        if not key or not value or len(key) > 60 or key in SKIPPED or key.startswith(SKIPPED_PREFIXES):
            continue
        facts[key] = value[:300]
    raw = {str(attribute.get("id")): _value(attribute) for attribute in attributes or [] if isinstance(attribute, dict)}
    kept = []
    for url in pictures:
        if url and url.startswith(("http://", "https://")) and url not in kept and len(url) <= 1024:
            kept.append(url)
    return {
        "title": " ".join(str(name or "").split())[:200],
        "brand_hint": raw.get("BRAND", "")[:100],
        "variant": _variant(raw.get("UNIT_VOLUME", "")),
        "gtin": _gtin(raw.get("GTIN", "")),
        "attributes": dict(list(facts.items())[:100]),
        "pictures": kept[:MAX_IMAGES],
        "description": (description or "").strip()[:5000],
        "meli_catalog_product_id": catalog_product_id[:32],
        "meli_category_id": category_id[:32],
    }


def _variant(volume):
    """"30 mL" → 30ML, when that makes a valid variant code."""
    code = normalize_code(re.sub(r"[^0-9A-Za-z]", "", volume or ""))
    try:
        validate_variant(code)
    except ValidationError:
        return ""
    return code


def _gtin(value):
    digits = re.search(r"\d{8,14}", value or "")
    if not digits:
        return ""
    try:
        validate_gtin(digits.group(0))
    except ValidationError:
        return ""
    return digits.group(0)
