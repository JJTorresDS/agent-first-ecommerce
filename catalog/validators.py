from django.core.exceptions import ValidationError

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
