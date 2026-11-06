import re

from django.core.exceptions import ValidationError
from django.utils.deconstruct import deconstructible

ATTRIBUTE_KEY_LIMIT = 60
ATTRIBUTE_COUNT_LIMIT = 100
_SCALARS = (str, int, float, bool)


def validate_attributes(value):
    """Secondary attributes: a flat object of scalars or lists of scalars.

    Mercado Libre style ids such as COLOR or VOLUME_ML are recommended as keys.
    """
    if not isinstance(value, dict):
        raise ValidationError("Attributes must be a JSON object.")
    if len(value) > ATTRIBUTE_COUNT_LIMIT:
        raise ValidationError(f"Use at most {ATTRIBUTE_COUNT_LIMIT} attributes.")
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip() or len(key) > ATTRIBUTE_KEY_LIMIT:
            raise ValidationError(
                f"Attribute names must be text of 1 to {ATTRIBUTE_KEY_LIMIT} characters."
            )
        if item is None or isinstance(item, _SCALARS):
            continue
        if isinstance(item, list) and all(isinstance(part, _SCALARS) for part in item):
            continue
        raise ValidationError(
            f"Attribute {key} must be text, a number, true/false, or a list of those."
        )


def validate_gtin(value):
    """An EAN/UPC barcode: 8, 12, 13 or 14 digits."""
    if value and (not value.isdigit() or len(value) not in {8, 12, 13, 14}):
        raise ValidationError("GTIN must be 8, 12, 13 or 14 digits.")


def validate_currency(value):
    if not (len(value) == 3 and value.isalpha() and value.isupper()):
        raise ValidationError("Currency must be a 3-letter code such as ARS or USD.")


def normalize_code(value):
    """Codes are stored in capitals without surrounding spaces."""
    return (value or "").strip().upper()


@deconstructible
class CodeValidator:
    """A SKU segment: capital letters and digits only. The hyphen joins segments."""

    def __init__(self, label, low, high):
        self.label = label
        self.low = low
        self.high = high

    def __call__(self, value):
        if not (self.low <= len(value) <= self.high) or not all(
            char.isascii() and char.isalnum() and not char.islower() for char in value
        ):
            raise ValidationError(
                f"{self.label} must be {self.low} to {self.high} capital letters or digits, "
                "without spaces or hyphens."
            )

    def __eq__(self, other):
        return isinstance(other, CodeValidator) and (self.label, self.low, self.high) == (
            other.label,
            other.low,
            other.high,
        )


validate_category_code = CodeValidator("Category code", 2, 6)
validate_brand_code = CodeValidator("Brand code", 2, 8)
validate_model_code = CodeValidator("Model code", 1, 10)
validate_variant = CodeValidator("Variant", 1, 10)
validate_combo_code = CodeValidator("Combo code", 1, 10)


_CATEGORY = re.compile(r"^\s*([A-Za-z]{3}\d+)")


def clean_category_id(value):
    """Only the Mercado Libre category code: "MLA1271 (Perfumes)" becomes MLA1271. Empty stays empty."""
    value = (value or "").strip()
    if not value:
        return ""
    found = _CATEGORY.match(value)
    if not found:
        raise ValidationError("Enter a Mercado Libre category code like MLA1271, or pick one of the suggestions.")
    return found.group(1).upper()
