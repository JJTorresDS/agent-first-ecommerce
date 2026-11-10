"""AI suggestions the user reviews before saving: SKU codes, descriptions, attributes, and a fix for a refused listing.

Every suggested code goes through the same validators as a typed one; an invalid one is dropped.
"""

from django.core.exceptions import ValidationError

from vimple import ai

from .models import Brand, Category
from .validators import (
    normalize_code,
    validate_brand_code,
    validate_category_code,
    validate_model_code,
    validate_variant,
)

CODES_PROMPT = (
    "You help a seller in Argentina add a product to their catalog. Its SKU is "
    "CATEGORY-BRAND-MODEL-VARIANT, for example PERF-ONLY-885-30ML.\n"
    "Codes use only capital letters A-Z and digits 0-9, no spaces or hyphens: category 2-6, "
    "brand 2-8, model 1-10, variant 1-10 characters. The variant is what sets this version apart "
    "(30ML, ROJO, XL), or UNICO when there is only one.\n"
    "Reuse an existing category or brand code from the lists when one fits; otherwise propose a new "
    "short code and a name for it.\n"
    "Return ONLY a JSON object with: category_code, category_name, brand_code, brand_name, "
    "model_code, model_name (the product's name without the variant), variant."
)

DESCRIPTION_PROMPT = (
    "You write Mercado Libre product descriptions in Spanish (Argentina) for the seller's own store.\n"
    "Write 1 to 3 short plain-text paragraphs using only facts in the given data. Don't invent "
    "features, don't mention other sellers, stores, prices, shipping or warranties, no emojis, no "
    "ALL CAPS. Return ONLY a JSON object: {\"description\": \"...\"}."
)

SELLER_NOTES = (
    "\nseller_notes, when given, are the seller's wishes for tone or occasion (a holiday, a gift): follow "
    "them, but still invent no product facts, prices, discounts or shipping promises."
)
EXTRA_LIMIT = 500


def _notes(extra):
    """The seller's extra words for a description, trimmed; nothing when empty."""
    extra = " ".join(str(extra or "").split())[:EXTRA_LIMIT]
    return {"seller_notes": extra} if extra else {}


_CODES = {
    "category_code": validate_category_code,
    "brand_code": validate_brand_code,
    "model_code": validate_model_code,
    "variant": validate_variant,
}
_NAMES = {"category_name": 100, "brand_name": 100, "model_name": 200}


def _product(name, brand, attributes):
    return {"name": (name or "")[:300], "brand": (brand or "")[:100], "attributes": attributes or {}}


def suggest_codes(organization, name, brand="", attributes=None):
    """Valid codes (and names) for a new product, reusing the organization's own codes where they fit."""
    answer = ai.complete_json(
        CODES_PROMPT,
        {
            "product": _product(name, brand, attributes),
            "existing_categories": [
                {"code": row.code, "name": row.name} for row in Category.objects.filter(organization=organization)
            ],
            "existing_brands": [
                {"code": row.code, "name": row.name} for row in Brand.objects.filter(organization=organization)
            ],
        },
    )
    values = {}
    for key, validate in _CODES.items():
        code = normalize_code(str(answer.get(key) or ""))
        try:
            validate(code)
        except ValidationError:
            continue
        values[key] = code
    for key, limit in _NAMES.items():
        text = " ".join(str(answer.get(key) or "").split())[:limit]
        if text:
            values[key] = text
    return values


def write_description(name, brand="", attributes=None, extra=""):
    answer = ai.complete_json(
        DESCRIPTION_PROMPT + SELLER_NOTES, {"product": _product(name, brand, attributes), **_notes(extra)}
    )
    text = str(answer.get("description") or "").strip()
    return text[:5000]


ATTRIBUTES_PROMPT = (
    "You fill Mercado Libre listing attributes for a product, in Spanish (Argentina).\n"
    "You get the product's data and the attributes to fill: id, name, value_type, allowed_values "
    "(Mercado Libre's own list for that attribute, read from its API) and, when there is one, the "
    "current value Mercado Libre refused. Use only facts from the product data.\n"
    "When allowed_values is not empty, answer with exactly one of them, copied as written; any other "
    "value is refused. Never repeat a refused current value. Skip any attribute you can't fill with "
    "confidence; never guess.\n"
    "Return ONLY a JSON object mapping attribute id to value, for example {\"PERFUME_TYPE\": \"Eau de parfum\"}."
)


def fill_attributes(name, brand, attributes, missing):
    """Values for missing or refused attributes ([{id, name, type, values, value?}]).

    Only values on Mercado Libre's list, in its spelling, when the attribute has one.
    """
    answer = ai.complete_json(
        ATTRIBUTES_PROMPT,
        {
            "product": _product(name, brand, attributes),
            "attributes_to_fill": [
                {
                    "id": item["id"],
                    "name": item.get("name", ""),
                    "value_type": item.get("type", ""),
                    "allowed_values": item.get("values") or [],
                    **({"current_value_refused": item["value"]} if item.get("value") else {}),
                }
                for item in missing
            ],
        },
    )
    filled = {}
    for item in missing:
        value = " ".join(str(answer.get(item["id"]) or "").split())[:100]
        if not value:
            continue
        allowed = item.get("values") or []
        if allowed:
            match = next((option for option in allowed if option.lower() == value.lower()), None)
            if match is None:
                continue
            value = match
        filled[item["id"]] = value
    return filled


COMBO_DESCRIPTION_PROMPT = (
    "You write Mercado Libre descriptions in Spanish (Argentina) for a kit the seller sells as one listing.\n"
    "You get the kit's title and each product in it: how many, its title, brand, its own description and "
    "attributes. Write 1 to 3 short plain-text paragraphs: say what the kit contains (with quantities), then "
    "what each product is, using only facts from the given descriptions and attributes. Don't invent "
    "features, don't mention prices, shipping, warranties, other sellers or stores, no emojis, no ALL CAPS.\n"
    "Return ONLY a JSON object: {\"description\": \"...\"}."
)


def write_combo_description(title, lines, extra=""):
    """A kit's description from its products' own data. lines: [(product, quantity)] of one organization."""
    components = []
    for product, quantity in lines:
        model = product.product_model
        components.append(
            {
                "quantity": quantity,
                "title": product.title[:200],
                "brand": model.brand.name[:100],
                "description": (model.description or "")[:1500],
                "attributes": product.attributes or {},
            }
        )
    answer = ai.complete_json(
        COMBO_DESCRIPTION_PROMPT + SELLER_NOTES,
        {"kit_title": (title or "")[:200], "products": components, **_notes(extra)},
    )
    return str(answer.get("description") or "").strip()[:5000]


ADAPT_PROMPT = (
    "You prepare Mercado Libre listing attributes for a new product, in Spanish (Argentina), by adapting "
    "the attributes of one of the seller's own listings in the same category.\n"
    "You get source_listing (its title and attributes), target_product (title, brand, description, "
    "attributes) and, per attribute id, Mercado Libre's value_type and allowed_values.\n"
    "For each source attribute decide:\n"
    "- keep its value when it is true of the target too (gender, product type, concentration, country, "
    "age group, format);\n"
    "- rewrite it from the target's own data when it describes the source product itself (its name, line, "
    "model, volume, color, fragrance);\n"
    "- drop it when the target data can't support a value.\n"
    "When allowed_values is not empty, use exactly one of them, copied as written. A number_unit value "
    "is a number and a unit, like 30 mL. Never invent facts the target data doesn't give.\n"
    "Return ONLY a JSON object: {\"values\": {attribute id: value}, \"dropped\": {attribute id: short reason}}."
)


def adapt_attributes(source_title, source, target, schema):
    """Values for a new listing from another listing's attributes: ({id: value}, {id: reason dropped}).

    source: [{id, name, value}] from one of the seller's listings; target: the new product's own data;
    schema: {id: {type, values}} from Mercado Libre. Only values on its list (in its spelling) are kept.
    """
    from meli.drafts import NUMBER_UNIT

    answer = ai.complete_json(
        ADAPT_PROMPT,
        {
            "source_listing": {"title": (source_title or "")[:300], "attributes": source},
            "target_product": target,
            "attribute_rules": {
                item["id"]: {"value_type": schema.get(item["id"], {}).get("type", ""),
                             "allowed_values": schema.get(item["id"], {}).get("values") or []}
                for item in source
            },
        },
    )
    asked = {item["id"] for item in source}
    values, dropped = {}, {}
    answered = answer.get("values") if isinstance(answer.get("values"), dict) else {}
    for key, raw in answered.items():
        value = " ".join(str(raw or "").split())[:100]
        if key not in asked or not value:
            continue
        rules = schema.get(key, {})
        allowed = rules.get("values") or []
        if allowed:
            value = next((option for option in allowed if option.lower() == value.lower()), None)
            if value is None:
                dropped[key] = "not one of Mercado Libre's values"
                continue
        elif rules.get("type") == "number_unit" and not NUMBER_UNIT.match(value):
            dropped[key] = "needs a number and a unit"
            continue
        values[key] = value
    reasons = answer.get("dropped") if isinstance(answer.get("dropped"), dict) else {}
    for key in asked - set(values) - set(dropped):
        dropped[key] = " ".join(str(reasons.get(key) or "describes the other product").split())[:120]
    return values, dropped


FIX_PROMPT = (
    "You fix a Mercado Libre Argentina listing body that Mercado Libre refused.\n"
    "You get: sent (the body sent to POST /items/validate), response (Mercado Libre's answer, with every "
    "cause), schema (the category's attributes: id, name, required, value_type, allowed_values, units), "
    "user_product_seller (the seller's tag), catalog_listing, and the product's own data.\n"
    "Return the corrected body: the same JSON as sent, with only the changes needed to clear every cause of "
    "type error. Keep everything else exactly as sent. Never change price, available_quantity, currency_id, "
    "category_id, catalog_product_id, catalog_listing, listing_type_id, buying_mode, pictures or SELLER_SKU.\n"
    "Attributes are {\"id\", \"value_name\"} or {\"id\", \"value_id\"}. When an attribute has allowed_values, "
    "use one of them as value_name, copied as written. Measures are a number and a unit: \"12 cm\", \"320 g\", "
    "\"75 mL\".\n"
    "A user_product_seller sends family_name instead of title on a regular listing, and neither on a "
    "catalog listing.\n"
    "When a cause needs a fact you can't read from the data (package size or weight, GTIN, a measure), give "
    "your best realistic estimate for this product as it ships, boxed, and mark that change estimated. "
    "Package dimensions in Mercado Libre's answer are too small when they are below the product's own.\n"
    "Return ONLY a JSON object: {\"body\": {…the whole corrected body…}, \"changes\": [{\"field\": "
    "\"SELLER_PACKAGE_WEIGHT\", \"why\": \"one short sentence in English\", \"estimated\": true}]}. "
    "field is the top-level key or the attribute id."
)


def fix_listing(sent, response, schema, user_product_seller, name, brand, attributes, package):
    """Mistral's corrected body for a refused listing and its reasons: ({body}, [{field, why, estimated}]).

    Gets the listing data only: never tokens or costs. The caller keeps the fields Vimple owns.
    """
    answer = ai.complete_json(
        FIX_PROMPT,
        {
            "sent": sent,
            "response": response,
            "schema": [
                {
                    "id": item.get("id"),
                    "name": item.get("name", ""),
                    "required": bool(item.get("required")),
                    "value_type": item.get("type", ""),
                    "allowed_values": (item.get("values") or [])[:60],
                    "units": item.get("units") or [],
                }
                for item in schema or []
                if not item.get("hidden")
            ],
            "user_product_seller": bool(user_product_seller),
            "catalog_listing": bool(sent.get("catalog_listing")),
            "product": {**_product(name, brand, attributes), "package": package or {}},
        },
    )
    body = answer.get("body")
    if not isinstance(body, dict):
        raise ai.SuggestionFailed("no body")
    changes = [item for item in answer.get("changes") or [] if isinstance(item, dict)][:40]
    return body, changes
