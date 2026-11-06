"""AI suggestions for catalog forms: SKU codes and a description. The user reviews them before saving.

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


def write_description(name, brand="", attributes=None):
    answer = ai.complete_json(DESCRIPTION_PROMPT, {"product": _product(name, brand, attributes)})
    text = str(answer.get("description") or "").strip()
    return text[:5000]


ATTRIBUTES_PROMPT = (
    "You fill Mercado Libre listing attributes for a product, in Spanish (Argentina).\n"
    "You get the product's data and the attributes to fill: id, name, and allowed values when there "
    "is a list. Use only facts from the product data. When an attribute has allowed values, answer "
    "with exactly one of them. Skip any attribute you can't fill with confidence; never guess.\n"
    "Return ONLY a JSON object mapping attribute id to value, for example {\"PERFUME_TYPE\": \"Eau de parfum\"}."
)


def fill_attributes(name, brand, attributes, missing):
    """Values for missing attributes ([{id, name, values}]): only from the allowed list when there is one."""
    answer = ai.complete_json(
        ATTRIBUTES_PROMPT,
        {
            "product": _product(name, brand, attributes),
            "attributes_to_fill": [
                {"id": item["id"], "name": item.get("name", ""), "allowed_values": item.get("values") or []}
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
