from decimal import Decimal

from django import forms
from django.core.validators import MinValueValidator

from catalog.models import MasterProduct

from .drafts import DraftError, catalog_product_id
from .models import ListingDefaults, ListingDraft, ListingSettings

MIN_PRICE = Decimal("0.01")


class ListingDefaultsForm(forms.ModelForm):
    class Meta:
        model = ListingDefaults
        fields = list(ListingSettings.SETTING_FIELDS)


def _quantity_problem(quantity, available):
    if available < 1:
        return "No stock left in the Depósito."
    if quantity > available:
        return f"Only {available} in stock in the Depósito."
    return ""


class StockedProductField(forms.ModelChoiceField):
    def __init__(self, *args, stock=None, **kwargs):
        self.stock = stock or {}
        super().__init__(*args, **kwargs)

    def label_from_instance(self, product):
        return f"{product.sku} · {product.title} ({self.stock.get(product.pk, 0)} in stock)"


class CatalogDraftForm(forms.Form):
    """Paste a catalog product URL and pick the Vimple product it sells. The organization comes from the view."""

    url = forms.URLField(label="Catalog product URL", max_length=1024, assume_scheme="https")
    product = StockedProductField(
        queryset=MasterProduct.objects.none(),
        label="Vimple product",
        required=False,
        empty_label="The product imported from this link",
    )
    price = forms.DecimalField(max_digits=14, decimal_places=2, min_value=MIN_PRICE)
    quantity = forms.IntegerField(min_value=1, initial=1)

    def __init__(self, *args, organization, stock, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization
        self.stock = stock
        self.import_url = ""
        in_stock = [product_id for product_id, units in stock.items() if units > 0]
        field = self.fields["product"]
        field.stock = stock
        # Only this organization's products, and only those with stock to sell.
        field.queryset = (
            MasterProduct.objects.filter(organization=organization, pk__in=in_stock)
            .exclude(status=MasterProduct.Status.ARCHIVED)
            .order_by("sku")
        )

    def clean_url(self):
        url = self.cleaned_data["url"]
        try:
            self.catalog_product_id = catalog_product_id(url)
        except DraftError as error:
            raise forms.ValidationError(str(error)) from None
        return url

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("product") is None and getattr(self, "catalog_product_id", ""):
            cleaned["product"] = self._product_for_link(cleaned.get("url"))
        product, quantity = cleaned.get("product"), cleaned.get("quantity")
        if product is not None and quantity is not None:
            problem = _quantity_problem(quantity, self.stock.get(product.pk, 0))
            if problem:
                self.add_error("quantity", problem)
        return cleaned

    def _product_for_link(self, url):
        """No product picked: the one imported from this catalog page, in this organization."""
        match = (
            MasterProduct.objects.filter(organization=self.organization, meli_catalog_product_id=self.catalog_product_id)
            .exclude(status=MasterProduct.Status.ARCHIVED)
            .first()
        )
        if match is None:
            self.import_url = url
            self.add_error("product", "No Vimple product came from this link. Pick one, or import it as a new product.")
        elif not self.fields["product"].queryset.filter(pk=match.pk).exists():
            self.add_error("product", f"{match.sku} came from this link but has no stock in the Depósito.")
            return None
        return match


class DraftForm(forms.ModelForm):
    """Price, quantity, and either the defaults or this listing's own settings."""

    class Meta:
        model = ListingDraft
        fields = ["price", "quantity", "category_id", "template", "use_defaults", *ListingSettings.SETTING_FIELDS]
        labels = {"category_id": "Mercado Libre category", "template": "Template for this category"}
        help_texts = {
            "category_id": "Pick one of Mercado Libre's suggestions; only the code (MLA…) is saved. "
            "Empty: Mercado Libre picks one from the title."
        }

    def __init__(self, *args, available, **kwargs):
        super().__init__(*args, **kwargs)
        self.available = available
        self.fields["price"].validators.append(MinValueValidator(MIN_PRICE))
        self.fields["quantity"].min_value = 1
        self.fields["quantity"].widget.attrs.update({"min": 1, "max": max(available, 1)})
        self.fields["use_defaults"].label = "Use the defaults"
        if self.instance.regular():
            self.fields["template"].help_text = (
                "Posted and paused at once, with quantity 1: never for sale. New listings in this category "
                "copy its attributes. Fill it in yourself; the AI buttons are off for a template."
            )
        else:
            del self.fields["template"]
        self.fields["category_id"].widget.attrs.update(
            {"list": "category-suggestions", "autocomplete": "off", "placeholder": "MLA1271"}
        )

    def clean_category_id(self):
        from catalog.validators import clean_category_id

        return clean_category_id(self.cleaned_data.get("category_id"))

    def clean_quantity(self):
        if self.data.get("template") and "template" in self.fields:
            # A template sells nothing: one unit, whatever the stock.
            return 1
        quantity = self.cleaned_data["quantity"]
        if quantity < 1:
            raise forms.ValidationError("At least 1.")
        problem = _quantity_problem(quantity, self.available)
        if problem:
            raise forms.ValidationError(problem)
        return quantity
