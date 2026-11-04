import re

from django import forms

from .models import Brand, Category, MasterProduct, ProductModel


_NUMBER = re.compile(r"-?(0|[1-9][0-9]*)([.,][0-9]+)?")


class TenantForm(forms.ModelForm):
    """A model form whose organization comes from the view, never from the request.

    The organization (and other fields the view fills) still take part in the
    unique checks, so a duplicate code is a form error, not a database error.
    """

    checked_fields = ("organization",)

    def _get_validation_exclusions(self):
        exclude = super()._get_validation_exclusions()
        for field in self.checked_fields:
            exclude.discard(field)
        return exclude


class CategoryForm(TenantForm):
    class Meta:
        model = Category
        fields = ["code", "name"]


class BrandForm(TenantForm):
    class Meta:
        model = Brand
        fields = ["code", "name"]


class ProductModelForm(TenantForm):
    class Meta:
        model = ProductModel
        fields = ["category", "brand", "code", "name", "condition", "description"]
        widgets = {"description": forms.Textarea(attrs={"rows": 4})}
        help_texts = {
            "code": "Short code, unique within the brand. Example: 885",
            "name": "The name people read. Example: Only 885 Eau de Parfum",
        }

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = Category.objects.filter(organization=organization)
        self.fields["brand"].queryset = Brand.objects.filter(organization=organization)
        self.locked = bool(self.instance.pk and self.instance.products.exists())
        if self.locked:
            # Variants already carry these codes in their SKUs.
            for name in ("category", "brand", "code"):
                self.fields[name].disabled = True


class VariantForm(TenantForm):
    """A new variant of a model. The SKU is built from the model's codes and this variant."""

    checked_fields = ("organization", "product_model", "sku")

    class Meta:
        model = MasterProduct
        fields = ["variant", "title", "image_url", "gtin", "list_price", "currency", "status"]


class ProductForm(TenantForm):
    """Everything about a variant that can change. Model, variant and SKU stay as they are."""

    class Meta:
        model = MasterProduct
        fields = [
            "title",
            "status",
            "image_url",
            "gtin",
            "list_price",
            "currency",
            "weight_g",
            "length_cm",
            "width_cm",
            "height_cm",
        ]


LIST_SEPARATOR = ";"


def attributes_from(post):
    """The attribute rows from the product page.

    100 and 4,5 become numbers, true/false become booleans, and "a; b" becomes a list.
    Everything else, including codes such as 007, stays text.
    """
    attributes = {}
    for key, value in zip(post.getlist("attr_key"), post.getlist("attr_value")):
        key = key.strip()
        if not key:
            continue
        if LIST_SEPARATOR in value:
            attributes[key] = [_scalar(part.strip()) for part in value.split(LIST_SEPARATOR) if part.strip()]
        else:
            attributes[key] = _scalar(value.strip())
    return attributes


def attribute_rows(attributes):
    """(key, text) pairs for the editor, written so attributes_from reads them back the same."""
    return [(key, _text(value)) for key, value in attributes.items()]


def _text(value):
    if isinstance(value, list):
        return f"{LIST_SEPARATOR} ".join(_text(part) for part in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


def _scalar(value):
    """100 and 4.5 (or 4,5) become numbers, true/false booleans. Codes such as 007 stay text."""
    if value in {"true", "false"}:
        return value == "true"
    if not _NUMBER.fullmatch(value):
        return value
    value = value.replace(",", ".")
    return float(value) if "." in value else int(value)
