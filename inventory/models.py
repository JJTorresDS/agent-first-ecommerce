from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from accounts.models import TenantModel


class StockLocation(TenantModel):
    """Where units are: the organization's own Depósito, or Mercado Libre Full."""

    class Kind(models.TextChoices):
        OWN = "own", "Depósito"
        MELI_FULL = "meli_full", "Mercado Libre Full"

    name = models.CharField(max_length=80)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "inventory_location"
        ordering = ["kind"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "kind"], name="uniq_inventory_org_location_kind"),
        ]

    def __str__(self):
        return self.name


class InventorySettings(models.Model):
    """started_at is the first stock count. Mercado Libre sales before it are already in that count."""

    organization = models.OneToOneField(
        "accounts.Organization",
        on_delete=models.CASCADE,
        related_name="inventory_settings",
    )
    started_at = models.DateTimeField(null=True, blank=True)
    started_by = models.CharField(max_length=254, blank=True)

    class Meta:
        db_table = "inventory_settings"


class StockMovement(TenantModel):
    """One change in stock. On-hand is the sum of these; nothing else writes stock."""

    class Kind(models.TextChoices):
        OPENING = "opening", "Opening count"
        COUNT = "count", "Count"
        RECEIPT = "receipt", "Purchase received"
        SALE = "sale", "Mercado Libre sale"
        SALE_CANCEL = "sale_cancel", "Sale cancelled"
        TRANSFER_OUT = "transfer_out", "Sent to Full"
        TRANSFER_IN = "transfer_in", "Arrived at Full"
        RETURN = "return", "Return"
        DAMAGE = "damage", "Damage or loss"
        ADJUSTMENT = "adjustment", "Adjustment"

    product = models.ForeignKey(
        "catalog.MasterProduct",
        on_delete=models.PROTECT,
        related_name="stock_movements",
    )
    location = models.ForeignKey(StockLocation, on_delete=models.PROTECT, related_name="movements")
    quantity = models.IntegerField(help_text="Units in (positive) or out (negative).")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    occurred_at = models.DateTimeField(default=timezone.now)
    note = models.CharField(max_length=300, blank=True)
    created_by = models.CharField(max_length=254, blank=True)
    purchase_item = models.ForeignKey(
        "purchasing.PurchaseItem",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="receipts",
    )
    meli_sale = models.ForeignKey(
        "meli.MeliSale",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="movements",
    )
    source_key = models.CharField(
        max_length=160,
        blank=True,
        help_text="Set on automatic movements, so the same order or receipt is never counted twice.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "inventory_movement"
        ordering = ["-occurred_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "source_key"],
                condition=~models.Q(source_key=""),
                name="uniq_inventory_org_source_key",
            ),
            models.CheckConstraint(condition=~models.Q(quantity=0), name="inventory_movement_not_zero"),
        ]
        indexes = [
            models.Index(fields=["organization", "product", "location"], name="inventory_org_product_loc"),
            models.Index(fields=["organization", "occurred_at"], name="inventory_org_occurred"),
        ]

    def __str__(self):
        return f"{self.quantity:+d} {self.product_id} {self.kind}"

    def _check(self):
        if not self.quantity:
            raise ValidationError({"quantity": "A movement moves at least one unit."})
        for field in ("product", "location", "purchase_item", "meli_sale"):
            if getattr(self, f"{field}_id") is None:
                continue
            if getattr(self, field).organization_id != self.organization_id:
                raise ValidationError({field: "This belongs to another organization."})

    def clean(self):
        super().clean()
        self._check()

    def save(self, *args, **kwargs):
        self._check()
        super().save(*args, **kwargs)
