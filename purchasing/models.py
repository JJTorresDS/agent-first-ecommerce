from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models

from accounts.models import TenantModel
from catalog.validators import validate_currency


class Supplier(TenantModel):
    name = models.CharField(max_length=200)
    tax_id = models.CharField("Tax id", max_length=32, blank=True, help_text="CUIT or RUT.")
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "purchasing_supplier"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name"],
                name="uniq_purchasing_org_supplier_name",
            ),
        ]

    def __str__(self):
        return self.name


class Purchase(TenantModel):
    """One invoice or order from a supplier. Its lines say what was bought and for how much."""

    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="purchases")
    purchased_on = models.DateField()
    invoice_number = models.CharField(max_length=64, blank=True)
    currency = models.CharField(max_length=3, default="ARS", validators=[validate_currency])
    exchange_rate = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        null=True,
        blank=True,
        help_text="Rate to ARS on the purchase date, when the currency is not ARS.",
    )
    cashflow_movement = models.ForeignKey(
        "tasks.CashflowMovement",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="purchases",
        help_text="The cashflow receipt this purchase was paid with.",
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "purchasing_purchase"
        ordering = ["-purchased_on", "-created_at"]

    def __str__(self):
        label = self.invoice_number or self.purchased_on
        return f"{self.supplier} · {label}"

    def clean(self):
        super().clean()
        _same_organization(self, "supplier", "cashflow_movement")

    def save(self, *args, **kwargs):
        _same_organization(self, "supplier", "cashflow_movement")
        super().save(*args, **kwargs)

    def total(self):
        return sum(
            (line.quantity * line.unit_cost for line in self.items.all()),
            Decimal("0"),
        )


class PurchaseItem(TenantModel):
    """One product on a purchase, at the price paid to that supplier."""

    purchase = models.ForeignKey(Purchase, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(
        "catalog.MasterProduct",
        on_delete=models.PROTECT,
        related_name="purchase_items",
    )
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    unit_cost = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(0)],
        help_text="In the purchase currency.",
    )
    supplier_sku = models.CharField(max_length=64, blank=True)

    class Meta:
        db_table = "purchasing_purchase_item"
        ordering = ["pk"]

    def __str__(self):
        return f"{self.quantity} × {self.product}"

    def _take_purchase_organization(self):
        # The admin validates a line before its new purchase is saved, so read the object.
        if self.organization_id:
            return
        try:
            purchase = self.purchase
        except Purchase.DoesNotExist:
            return
        self.organization_id = purchase.organization_id

    def clean(self):
        self._take_purchase_organization()
        super().clean()
        _same_organization(self, "purchase", "product")

    def save(self, *args, **kwargs):
        self._take_purchase_organization()
        _same_organization(self, "purchase", "product")
        super().save(*args, **kwargs)


def _same_organization(row, *fields):
    """Refuse a link to another organization's supplier, receipt, purchase or product."""
    if row.organization_id is None:
        return
    for field in fields:
        related_id = getattr(row, f"{field}_id")
        if related_id is None:
            continue
        related = getattr(row, field)
        if related.organization_id != row.organization_id:
            raise ValidationError({field: "This belongs to another organization."})
