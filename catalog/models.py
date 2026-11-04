import uuid

from django.core.validators import MinValueValidator, URLValidator
from django.db import models

from accounts.models import TenantModel

from .validators import validate_attributes, validate_currency, validate_gtin


class MasterProduct(TenantModel):
    """One sellable SKU in the organization's own catalog.

    Core listing data are columns; secondary attributes go in attributes.
    Channel listings (Mercado Libre, Tiendanube) will point here.
    """

    class Condition(models.TextChoices):
        NEW = "new", "New"
        USED = "used", "Used"
        REFURBISHED = "refurbished", "Refurbished"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ACTIVE = "active", "Active"
        ARCHIVED = "archived", "Archived"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sku = models.CharField(max_length=64)
    title = models.CharField(max_length=200)
    family = models.CharField(
        max_length=64,
        blank=True,
        help_text="Same value on every variant of one item, like 50 ml and 100 ml.",
    )
    brand = models.CharField(max_length=120, blank=True)
    model = models.CharField(max_length=120, blank=True)
    gtin = models.CharField(
        "GTIN",
        max_length=14,
        blank=True,
        validators=[validate_gtin],
        help_text="EAN/UPC barcode.",
    )
    category = models.CharField(max_length=200, blank=True)
    condition = models.CharField(
        max_length=16,
        choices=Condition.choices,
        default=Condition.NEW,
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    description = models.TextField(blank=True)
    image_url = models.URLField(
        max_length=2048,
        validators=[URLValidator(schemes=["http", "https"])],
        help_text="The product image. Media is stored outside Vimple.",
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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "catalog_master_product"
        ordering = ["title", "sku"]
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
        ]
        indexes = [
            models.Index(fields=["organization", "family"], name="catalog_org_family"),
        ]

    def __str__(self):
        return f"{self.sku} · {self.title}"

    def variants(self):
        """The other SKUs of the same item, in this organization only."""
        if not self.family:
            return MasterProduct.objects.none()
        return MasterProduct.objects.filter(
            organization_id=self.organization_id,
            family=self.family,
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
