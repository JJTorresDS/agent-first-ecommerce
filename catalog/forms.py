import re
from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import transaction

from .models import Brand, Category, Combo, MasterProduct, ProductModel
from .validators import clean_category_id, validate_attributes


_NUMBER = re.compile(r"-?(0|[1-9][0-9]*)([.,][0-9]+)?")


CATEGORY_INPUT = {"list": "category-suggestions", "autocomplete": "off", "placeholder": "MLA1271"}


def _category_field(field):
    field.widget.attrs.update(CATEGORY_INPUT)
    field.help_text = "Pick one of Mercado Libre's suggestions; only the code (MLA…) is saved."


def _require_price(field):
    """A price is needed to sell: required and above zero in the form, whatever the column allows."""
    field.required = True
    field.validators.append(MinValueValidator(Decimal("0.01")))
    field.widget.attrs.update({"min": "0.01", "step": "0.01"})


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
    """Everything about a variant that can change. Model, variant and SKU stay as they are.

    The list price is required here (a listing needs it); the column stays nullable for older rows.
    """

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
            "meli_catalog_product_id",
            "meli_category_id",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _require_price(self.fields["list_price"])
        _category_field(self.fields["meli_category_id"])

    def clean_meli_category_id(self):
        return clean_category_id(self.cleaned_data.get("meli_category_id"))


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


class ImportForm(forms.Form):
    """A new product from values read on Mercado Libre, reviewed by the user.

    It makes the model too unless an existing one is picked. Every select is limited to the
    organization, and the model and product are checked like typed ones before anything is saved.
    """

    url = forms.CharField(widget=forms.HiddenInput, required=False, max_length=1024)
    brand_hint = forms.CharField(widget=forms.HiddenInput, required=False, max_length=100)
    meli_catalog_product_id = forms.CharField(widget=forms.HiddenInput, required=False, max_length=32)
    meli_category_id = forms.CharField(widget=forms.HiddenInput, required=False, max_length=32)
    category = forms.ModelChoiceField(queryset=Category.objects.none())
    brand = forms.ModelChoiceField(queryset=Brand.objects.none())
    product_model = forms.ModelChoiceField(
        queryset=ProductModel.objects.none(),
        required=False,
        label="Existing model",
        help_text="Or leave empty and give the new model's code and name.",
    )
    model_code = forms.CharField(required=False, max_length=10, label="New model code", help_text="Example: 885")
    model_name = forms.CharField(required=False, max_length=200, label="New model name")
    variant = forms.CharField(max_length=10, help_text="What sets it apart: 30ML, ROJO, XL. UNICO when there is one.")
    title = forms.CharField(max_length=200)
    gtin = forms.CharField(required=False, max_length=14, label="GTIN")
    list_price = forms.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"))
    description = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 5}))

    def __init__(self, *args, organization, pictures=(), attributes=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization
        self.pictures = list(pictures)
        self.attributes = attributes or {}
        self.fields["category"].queryset = Category.objects.filter(organization=organization)
        self.fields["brand"].queryset = Brand.objects.filter(organization=organization)
        self.fields["product_model"].queryset = ProductModel.objects.filter(organization=organization).select_related(
            "brand"
        )

    def clean(self):
        cleaned = super().clean()
        category, brand, model = cleaned.get("category"), cleaned.get("brand"), cleaned.get("product_model")
        if not self.pictures:
            self.add_error(None, "Pick at least one picture.")
        try:
            validate_attributes(self.attributes)
        except ValidationError as error:
            self.add_error(None, error)
        if category is None or brand is None:
            return cleaned
        if model is not None:
            if (model.category_id, model.brand_id) != (category.pk, brand.pk):
                self.add_error("product_model", "That model has another category or brand.")
                return cleaned
            self.new_model = None
        else:
            if not cleaned.get("model_code") or not cleaned.get("model_name"):
                self.add_error("model_code", "Pick an existing model or give the new one's code and name.")
                return cleaned
            model = ProductModel(
                organization=self.organization,
                category=category,
                brand=brand,
                code=cleaned["model_code"],
                name=cleaned["model_name"],
                description=cleaned.get("description") or "",
            )
            try:
                model.full_clean()
            except ValidationError as error:
                self._add_model_errors(error, {"code": "model_code", "name": "model_name"})
                return cleaned
            self.new_model = model
        product = MasterProduct(
            organization=self.organization,
            variant=cleaned.get("variant") or "",
            title=cleaned.get("title") or "",
            gtin=cleaned.get("gtin") or "",
            list_price=cleaned.get("list_price"),
            image_url=self.pictures[0] if self.pictures else "",
            attributes=self.attributes,
            meli_catalog_product_id=cleaned.get("meli_catalog_product_id") or "",
            meli_category_id=cleaned.get("meli_category_id") or "",
        )
        if self.new_model is None:
            product.product_model = model
        try:
            # A new model isn't saved yet: its products can't clash on (model, variant).
            product.full_clean(exclude=["product_model"] if self.new_model else None)
        except ValidationError as error:
            self._add_model_errors(error, {"variant": "variant", "gtin": "gtin", "title": "title"})
            return cleaned
        self.product = product
        return cleaned

    def _add_model_errors(self, error, fields):
        for field, messages in getattr(error, "message_dict", {"__all__": error.messages}).items():
            target = fields.get(field)
            for message in messages:
                self.add_error(target if target in self.fields else None, message)

    @transaction.atomic
    def save(self):
        from .images import set_images

        if self.new_model is not None:
            self.new_model.save()
            self.product.product_model = self.new_model
        elif self.cleaned_data.get("description") and not self.product.product_model.description:
            self.product.product_model.description = self.cleaned_data["description"]
            self.product.product_model.save(update_fields=["description", "updated_at"])
        self.product.save()
        set_images(self.product, self.pictures)
        return self.product


class ComboForm(TenantForm):
    """A combo's own fields. The code makes the SKU, so it's fixed after the first save."""

    checked_fields = ("organization", "sku")

    class Meta:
        model = Combo
        fields = [
            "code", "title", "image_url", "status", "list_price", "currency", "description",
            "weight_g", "length_cm", "width_cm", "height_cm", "meli_category_id",
        ]
        widgets = {"description": forms.Textarea(attrs={"rows": 4})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _require_price(self.fields["list_price"])
        _category_field(self.fields["meli_category_id"])
        if self.instance.pk:
            self.fields["code"].disabled = True

    def clean_meli_category_id(self):
        return clean_category_id(self.cleaned_data.get("meli_category_id"))


MAX_COMBO_UNITS = 100


def read_combo_lines(post, organization):
    """[(product, quantity)] from the combo_sku / combo_quantity rows, and the problems to show.

    Products are found by SKU in this organization only; the same SKU twice adds up.
    """
    from collections import OrderedDict

    lines, errors = OrderedDict(), []
    skus = post.getlist("combo_sku")
    quantities = post.getlist("combo_quantity")
    for raw_sku, raw_quantity in zip(skus, quantities):
        sku = (raw_sku or "").split("·")[0].strip().upper()
        if not sku:
            continue
        try:
            quantity = int(raw_quantity)
        except (TypeError, ValueError):
            quantity = 0
        if not 1 <= quantity <= MAX_COMBO_UNITS:
            errors.append(f"{sku}: the quantity must be 1 to {MAX_COMBO_UNITS}.")
            continue
        product = (
            MasterProduct.objects.filter(organization=organization, sku=sku)
            .select_related("product_model__brand")
            .first()
        )
        if product is None:
            errors.append(f"{sku}: no product in the catalog has this SKU.")
            continue
        lines[product.pk] = (product, lines.get(product.pk, (product, 0))[1] + quantity)
    if not errors and sum(quantity for _, quantity in lines.values()) < 2:
        errors.append("A combo holds at least 2 units: two products, or one product twice.")
    return list(lines.values()), errors
