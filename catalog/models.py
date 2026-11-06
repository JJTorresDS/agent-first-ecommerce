import uuid

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, URLValidator
from django.db import models
from django.db.models.functions import Lower

from accounts.models import TenantModel

from .validators import (
    normalize_code,
    validate_attributes,
    validate_brand_code,
    validate_combo_code,
    validate_category_code,
    validate_currency,
    validate_gtin,
    validate_model_code,
    validate_variant,
)

CODE_IN_USE = "Products already use this code in their SKU. Create a new one instead."


class Condition(models.TextChoices):
    NEW = "new", "New"
    USED = "used", "Used"
    REFURBISHED = "refurbished", "Refurbished"


def _stored(row, *fields):
    """The saved values of these fields, or None for a new row."""
    if row._state.adding or row.pk is None:
        return None
    return type(row).objects.filter(pk=row.pk).values_list(*fields).first()


def _same_organization(row, *fields):
    """Refuse a link to another organization's category, brand or model."""
    if row.organization_id is None:
        return
    for field in fields:
        if getattr(row, f"{field}_id") is None:
            continue
        if getattr(row, field).organization_id != row.organization_id:
            raise ValidationError({field: "This belongs to another organization."})


class _CodedName(TenantModel):
    """A short code that goes in SKUs, and the name people read."""

    name = models.CharField(max_length=120)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True
        ordering = ["name"]

    def __str__(self):
        return f"{self.code} · {self.name}"

    def _in_use(self):
        raise NotImplementedError

    def clean_fields(self, exclude=None):
        # Capitalize before the code validator runs, so "perf" is accepted as PERF.
        self.code = normalize_code(self.code)
        super().clean_fields(exclude)

    def _check(self):
        self.code = normalize_code(self.code)
        self.name = (self.name or "").strip()
        stored = _stored(self, "code")
        if stored and stored[0] != self.code and self._in_use():
            raise ValidationError({"code": CODE_IN_USE})

    def clean(self):
        self._check()
        super().clean()

    def save(self, *args, **kwargs):
        self._check()
        super().save(*args, **kwargs)


class Category(_CodedName):
    code = models.CharField(max_length=6, validators=[validate_category_code], help_text="PERF")

    class Meta(_CodedName.Meta):
        db_table = "catalog_category"
        verbose_name_plural = "categories"
        constraints = [
            models.UniqueConstraint(fields=["organization", "code"], name="uniq_catalog_org_category_code"),
            models.UniqueConstraint(Lower("name"), "organization", name="uniq_catalog_org_category_name"),
        ]

    def _in_use(self):
        return MasterProduct.objects.filter(product_model__category=self).exists()


class Brand(_CodedName):
    code = models.CharField(max_length=8, validators=[validate_brand_code], help_text="ONLY")

    class Meta(_CodedName.Meta):
        db_table = "catalog_brand"
        constraints = [
            models.UniqueConstraint(fields=["organization", "code"], name="uniq_catalog_org_brand_code"),
            models.UniqueConstraint(Lower("name"), "organization", name="uniq_catalog_org_brand_name"),
        ]

    def _in_use(self):
        return MasterProduct.objects.filter(product_model__brand=self).exists()


class ProductModel(TenantModel):
    """One item and the family of its variants: PERF-ONLY-885 for the 30 ml and 100 ml bottles."""

    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="product_models")
    brand = models.ForeignKey(Brand, on_delete=models.PROTECT, related_name="product_models")
    code = models.CharField(
        max_length=10,
        validators=[validate_model_code],
        help_text="Short model code, unique within the brand: 885",
    )
    name = models.CharField(max_length=200, help_text="Only 885 Eau de Parfum")
    description = models.TextField(blank=True)
    condition = models.CharField(max_length=16, choices=Condition.choices, default=Condition.NEW)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "catalog_product_model"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "brand", "code"],
                name="uniq_catalog_org_brand_model_code",
            ),
        ]

    def __str__(self):
        return f"{self.family_code()} · {self.name}"

    def family_code(self):
        if self.category_id is None or self.brand_id is None:
            return self.code
        return f"{self.category.code}-{self.brand.code}-{self.code}"

    def clean_fields(self, exclude=None):
        self.code = normalize_code(self.code)
        super().clean_fields(exclude)

    def _check(self):
        self.code = normalize_code(self.code)
        self.name = (self.name or "").strip()
        _same_organization(self, "category", "brand")
        stored = _stored(self, "code", "category_id", "brand_id")
        if stored and stored != (self.code, self.category_id, self.brand_id) and self.products.exists():
            raise ValidationError(
                "Variants already use this model's code, category and brand in their SKU. "
                "Create a new model instead."
            )

    def clean(self):
        self._check()
        super().clean()

    def save(self, *args, **kwargs):
        self._check()
        super().save(*args, **kwargs)


class MasterProduct(TenantModel):
    """One sellable variant. Its SKU is CATEGORY-BRAND-MODEL-VARIANT, made once and never changed.

    Shared data lives on the ProductModel; secondary attributes go in attributes.
    Channel listings (Mercado Libre, Tiendanube) will point here.
    """

    Condition = Condition

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ACTIVE = "active", "Active"
        ARCHIVED = "archived", "Archived"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product_model = models.ForeignKey(
        ProductModel,
        on_delete=models.PROTECT,
        related_name="products",
        verbose_name="model",
    )
    variant = models.CharField(
        max_length=10,
        validators=[validate_variant],
        help_text="What sets this variant apart: 30ML, ROJO, XL. Use UNICO when there is one.",
    )
    sku = models.CharField("SKU", max_length=64, blank=True, editable=False)
    title = models.CharField(max_length=200, blank=True, help_text="Defaults to the model name and variant.")
    gtin = models.CharField(
        "GTIN",
        max_length=14,
        blank=True,
        validators=[validate_gtin],
        help_text="EAN/UPC barcode.",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    image_url = models.URLField(
        max_length=2048,
        validators=[URLValidator(schemes=["http", "https"])],
        help_text="The main picture: the first of its pictures. Media is stored outside Vimple.",
    )
    list_price = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
    )
    currency = models.CharField(max_length=3, default="ARS", validators=[validate_currency])
    weight_g = models.PositiveIntegerField("Weight (g)", null=True, blank=True)
    length_cm = models.DecimalField(
        "Length (cm)", max_digits=7, decimal_places=1, null=True, blank=True,
        validators=[MinValueValidator(0)],
    )
    width_cm = models.DecimalField(
        "Width (cm)", max_digits=7, decimal_places=1, null=True, blank=True,
        validators=[MinValueValidator(0)],
    )
    height_cm = models.DecimalField(
        "Height (cm)", max_digits=7, decimal_places=1, null=True, blank=True,
        validators=[MinValueValidator(0)],
    )
    attributes = models.JSONField(default=dict, blank=True, validators=[validate_attributes])
    meli_catalog_product_id = models.CharField(
        "Mercado Libre catalog product",
        max_length=32,
        blank=True,
        help_text="The catalog product it was imported from (MLA…), to list it in that catalog.",
    )
    meli_category_id = models.CharField("Mercado Libre category", max_length=32, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "catalog_master_product"
        ordering = ["sku"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "sku"],
                name="uniq_catalog_org_sku",
            ),
            models.UniqueConstraint(
                fields=["organization", "gtin"],
                condition=~models.Q(gtin=""),
                name="uniq_catalog_org_gtin",
            ),
            models.UniqueConstraint(
                fields=["product_model", "variant"],
                name="uniq_catalog_model_variant",
            ),
        ]

    def __str__(self):
        return f"{self.sku} · {self.title}"

    def clean_fields(self, exclude=None):
        self.variant = normalize_code(self.variant)
        super().clean_fields(exclude)

    def _fill(self):
        """Fill organization, SKU and title. The SKU never changes after the first save."""
        self.variant = normalize_code(self.variant)
        if self.product_model_id is None:
            return
        model = self.product_model
        if self.organization_id is None:
            self.organization_id = model.organization_id
        _same_organization(self, "product_model")
        stored = _stored(self, "product_model_id", "variant")
        if stored is None:
            self.sku = f"{model.family_code()}-{self.variant}"
        elif stored != (self.product_model_id, self.variant):
            raise ValidationError(
                "The model and variant make the SKU, so they can't change. "
                "Archive this product and create a new one."
            )
        if not (self.title or "").strip():
            self.title = f"{model.name} {self.variant}"[:200]

    def clean(self):
        self._fill()
        super().clean()

    def save(self, *args, **kwargs):
        self._fill()
        super().save(*args, **kwargs)

    def variants(self):
        """The other variants of the same model, in this organization only."""
        return MasterProduct.objects.filter(
            organization_id=self.organization_id,
            product_model_id=self.product_model_id,
        ).exclude(pk=self.pk)

    def costs_by_supplier(self):
        """The latest purchase line per supplier, newest purchase first.

        Each row has supplier, unit_cost, currency, purchased_on and purchase_id.
        """
        from purchasing.models import PurchaseItem

        lines = (
            PurchaseItem.objects.filter(
                organization_id=self.organization_id,
                product=self,
            )
            .select_related("purchase__supplier")
            .order_by("-purchase__purchased_on", "-purchase__created_at", "-pk")
        )
        latest = {}
        for line in lines:
            supplier = line.purchase.supplier
            if supplier.pk in latest:
                continue
            latest[supplier.pk] = {
                "supplier": supplier,
                "unit_cost": line.unit_cost,
                "currency": line.purchase.currency,
                "purchased_on": line.purchase.purchased_on,
                "purchase_id": line.purchase_id,
            }
        return list(latest.values())


MAX_IMAGES = 10


class ProductImage(TenantModel):
    """One picture of a product, by URL. Position 0 is the main one, also kept in MasterProduct.image_url.

    Write them through catalog.images.set_images, which keeps the two in step.
    """

    product = models.ForeignKey(MasterProduct, on_delete=models.CASCADE, related_name="images")
    url = models.URLField(max_length=1024, validators=[URLValidator(schemes=["http", "https"])])
    position = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "catalog_product_image"
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(fields=["product", "position"], name="uniq_catalog_image_position"),
        ]

    def __str__(self):
        return self.url

    def clean(self):
        if self.product_id and self.organization_id is None:
            self.organization_id = self.product.organization_id
        _same_organization(self, "product")
        super().clean()

    def save(self, *args, **kwargs):
        if self.organization_id is None:
            self.organization_id = self.product.organization_id
        _same_organization(self, "product")
        super().save(*args, **kwargs)


COMBO_WORDS = ("kit", "pack", "combo")


class Combo(TenantModel):
    """Several catalog products sold together as one listing (a Kit, Pack or Combo).

    Its SKU is COMBO-<CODE>, made once and never changed. A sale of its listing takes each
    component out of stock (inventory.ledger.apply_sale).
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ACTIVE = "active", "Active"
        ARCHIVED = "archived", "Archived"

    code = models.CharField(max_length=10, validators=[validate_combo_code], help_text="Example: 885X2")
    sku = models.CharField("SKU", max_length=64, blank=True, editable=False)
    title = models.CharField(max_length=200, help_text="Must say Kit, Pack or Combo: Kit Perfume 885 x2")
    image_url = models.URLField(
        max_length=2048,
        validators=[URLValidator(schemes=["http", "https"])],
        help_text="The combo's picture. Media is stored outside Vimple.",
    )
    list_price = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(0)]
    )
    currency = models.CharField(max_length=3, default="ARS", validators=[validate_currency])
    description = models.TextField(blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    weight_g = models.PositiveIntegerField("Weight (g)", null=True, blank=True)
    length_cm = models.DecimalField(
        "Length (cm)", max_digits=7, decimal_places=1, null=True, blank=True, validators=[MinValueValidator(0)]
    )
    width_cm = models.DecimalField(
        "Width (cm)", max_digits=7, decimal_places=1, null=True, blank=True, validators=[MinValueValidator(0)]
    )
    height_cm = models.DecimalField(
        "Height (cm)", max_digits=7, decimal_places=1, null=True, blank=True, validators=[MinValueValidator(0)]
    )
    attributes = models.JSONField(default=dict, blank=True, validators=[validate_attributes])
    meli_category_id = models.CharField("Mercado Libre category", max_length=32, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "catalog_combo"
        ordering = ["sku"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "sku"], name="uniq_catalog_org_combo_sku"),
        ]

    def __str__(self):
        return f"{self.sku} · {self.title}"

    def clean_fields(self, exclude=None):
        self.code = normalize_code(self.code)
        super().clean_fields(exclude)

    def _fill(self):
        self.code = normalize_code(self.code)
        stored = _stored(self, "code")
        if stored is None:
            self.sku = f"COMBO-{self.code}"
        elif stored != (self.code,):
            raise ValidationError({"code": "The code makes the SKU, so it can't change. Create a new combo."})

    def clean(self):
        self._fill()
        if not any(word in (self.title or "").lower() for word in COMBO_WORDS):
            raise ValidationError({"title": "Say Kit, Pack or Combo in the title, so buyers know it's several items."})
        if self.organization_id and MasterProduct.objects.filter(organization_id=self.organization_id, sku=self.sku).exists():
            raise ValidationError({"code": "A product already has this SKU."})
        super().clean()

    def save(self, *args, **kwargs):
        self._fill()
        super().save(*args, **kwargs)

    @property
    def gtin(self):
        """A combo has no barcode of its own, so it lists as brand Generic."""
        return ""

    def can_make(self, stock):
        """How many complete combos {product_id: units} allows: the scarcest component decides."""
        lines = list(self.items.all())
        if not lines:
            return 0
        return max(0, min(stock.get(line.product_id, 0) // line.quantity for line in lines))


class ComboItem(TenantModel):
    """One product in a combo and how many of it each combo holds."""

    combo = models.ForeignKey(Combo, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(MasterProduct, on_delete=models.PROTECT, related_name="combo_items")
    quantity = models.PositiveSmallIntegerField(default=1, validators=[MinValueValidator(1)])

    class Meta:
        db_table = "catalog_combo_item"
        ordering = ["pk"]
        constraints = [
            models.UniqueConstraint(fields=["combo", "product"], name="uniq_catalog_combo_product"),
        ]

    def __str__(self):
        return f"{self.quantity} × {self.product.sku}"

    def _check(self):
        if self.combo_id and self.organization_id is None:
            self.organization_id = self.combo.organization_id
        _same_organization(self, "combo", "product")

    def clean(self):
        self._check()
        super().clean()

    def save(self, *args, **kwargs):
        self._check()
        super().save(*args, **kwargs)
