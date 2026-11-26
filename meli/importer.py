"""A Mercado Libre product or listing, read through the API, as values for a new catalog product.

Nothing is saved here: the import page shows the values for the user to review. Reads only;
the token never leaves the server.
"""

import logging
import re
from urllib.parse import parse_qs, urlparse

from django.core.exceptions import ValidationError

from catalog.models import MAX_IMAGES
from catalog.validators import ATTRIBUTE_KEY_LIMIT, normalize_code, validate_gtin, validate_variant
from vimple import ai

from .client import fetch_catalog_product, fetch_item_description, fetch_items

logger = logging.getLogger(__name__)

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


def read_prospect(organization, prospect):
    """The values for a new product from a scraped listing, and a note for the page: (values, note).

    Its link is read through the API like a pasted one; when there's none Vimple can read, or Mercado Libre
    doesn't answer, the values come from the scrape. With the AI key, one Mistral call then maps the scrape
    onto the product's fields and the category's attributes (codes, title, GTIN, attributes). Nothing is saved.
    """
    from .client import MercadoLibreError, describe_failure
    from .drafts import DraftError, _reason

    notes = []
    values, link = None, _link_of(prospect)
    if link:
        try:
            values = read(organization, link)
        except (ImportProblem, DraftError, MercadoLibreError, OSError) as error:
            logger.warning("Scraped listing %s not read: %s", prospect.external_id, describe_failure(error))
            reason = str(error) if isinstance(error, ImportProblem) else _reason(error)
            notes.append(f"Mercado Libre couldn't read it ({reason.rstrip('.')}), so these values come from the scraped page. Check them.")
    else:
        notes.append("Its page isn't a link Mercado Libre's API reads, so these values come from the scraped page. Check them.")
    scraped = values is None
    if scraped:
        values = _from_scraped(prospect)
    values["list_price"] = prospect.price
    if ai.enabled():
        try:
            mapped = _map(organization, prospect, values)
        except ai.SuggestionFailed:
            notes.append("The AI couldn't map the scraped listing this time; fill in the codes yourself.")
        else:
            attributes = mapped.pop("attributes")
            if scraped and attributes:
                values["attributes"] = attributes
            else:
                values["attributes"] = {**attributes, **values["attributes"]}
            for key in ("title", "gtin"):
                if mapped.get(key) and (scraped or not values.get(key)):
                    values[key] = mapped.pop(key)
            if mapped.get("variant") and not values.get("variant"):
                values["variant"] = mapped["variant"]
            values["suggested"] = mapped
            notes.append("Codes and attributes are the AI's suggestion from the scraped listing: check them before saving.")
    return values, " ".join(notes)


def _link_of(prospect):
    """The first of the scraped page's links that Vimple can read through the API, else ""."""
    payload = prospect.payload if isinstance(prospect.payload, dict) else {}
    links = [prospect.url] + [payload.get(key) for key in ("pageUrl", "productUrl", "permalink")]
    if LISTING.fullmatch(prospect.external_id or ""):
        links.append(f"https://articulo.mercadolibre.com.ar/{prospect.external_id}")
    for link in links:
        try:
            source_of(str(link or ""))
        except ImportProblem:
            continue
        return str(link)
    return ""


def _from_scraped(prospect):
    """The same values read() gives, from what the scraper stored: no Mercado Libre call."""
    characteristics = prospect.characteristics()
    by_name = {name.strip().lower(): value for name, value in characteristics.items()}

    def named(*names):
        return next((by_name[name] for name in names if by_name.get(name)), "")

    gtin = next((value for name, value in by_name.items() if "gtin" in name or "código universal" in name), "")
    attributes = {}
    for name, value in characteristics.items():
        key = " ".join(name.split())[:ATTRIBUTE_KEY_LIMIT]
        if key and len(attributes) < 100:
            attributes[key] = value[:300]
    pictures = prospect.gallery() or ([prospect.picture] if prospect.picture else [])
    return {
        **_values(
            name=prospect.title,
            attributes=[],
            pictures=pictures,
            description=prospect.description(),
            catalog_product_id=prospect.catalog_product_id(),
            category_id=prospect.category_id,
        ),
        "brand_hint": named("marca", "brand")[:100],
        "variant": _variant(named("volumen", "volumen de la unidad", "contenido", "contenido neto")),
        "gtin": _gtin(gtin),
        "attributes": attributes,
    }


def _map(organization, prospect, values):
    """One Mistral call (with retries) mapping the scrape onto the product's fields: catalog.suggest.map_scraped."""
    from catalog.suggest import map_scraped

    schema = _category_schema(organization, values.get("meli_category_id") or prospect.category_id)
    asked = [
        {"id": item["id"], "name": item["name"], "value_type": item["type"], "allowed_values": item["values"][:60]}
        for item in schema
        if not item["hidden"]
        and item["id"] not in SKIPPED
        and not item["id"].startswith(SKIPPED_PREFIXES)
        and item["id"] not in values["attributes"]
    ][:120]
    payload = prospect.payload if isinstance(prospect.payload, dict) else {}
    extra = {
        str(key): value
        for key, value in payload.items()
        if isinstance(value, (str, int, float, bool)) and len(str(value)) <= 300 and key not in {"description", "descriptionText"}
    }
    scraped = {
        **extra,
        "title": prospect.title,
        "seller": prospect.seller,
        "characteristics": prospect.characteristics(),
        "description": prospect.description()[:3000],
    }
    read_values = {
        key: values.get(key) for key in ("title", "brand_hint", "variant", "gtin", "attributes")
    }
    return map_scraped(
        organization,
        scraped,
        read_values,
        asked,
        {item["id"]: {"type": item["type"], "values": item["values"]} for item in schema},
    )


def _category_schema(organization, category_id):
    """schema_of the category's attributes, or [] without a connection or an answer (logged)."""
    from .client import describe_failure, fetch_category_attributes
    from .drafts import _token, schema_of

    if not category_id:
        return []
    try:
        return schema_of(fetch_category_attributes(_token(organization), category_id))
    except Exception as error:
        logger.warning("Attributes of category %s not read: %s", category_id, describe_failure(error))
        return []


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
