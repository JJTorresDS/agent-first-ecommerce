from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django import forms
from django.utils import timezone

from catalog.forms import TenantForm
from catalog.models import MasterProduct
from tasks.models import CashflowMovement

from .models import Purchase, Supplier

RECEIPT_DAYS = 120
MAX_COST = Decimal("1000000000000")  # PurchaseItem.unit_cost holds 12 digits before the point
LINE_FIELDS = ("line_id", "line_product", "line_quantity", "line_unit_cost", "line_supplier_sku")


class SupplierForm(TenantForm):
    class Meta:
        model = Supplier
        fields = ["name", "tax_id", "email", "phone", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}


class PurchaseForm(TenantForm):
    class Meta:
        model = Purchase
        fields = ["supplier", "purchased_on", "invoice_number", "currency", "exchange_rate", "cashflow_movement", "notes"]
        widgets = {
            "purchased_on": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, organization, can_link_receipts, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["supplier"].queryset = Supplier.objects.filter(organization=organization)
        if not can_link_receipts:
            del self.fields["cashflow_movement"]
            return
        since = timezone.localdate() - timedelta(days=RECEIPT_DAYS)
        receipts = CashflowMovement.objects.filter(organization=organization, transaction_date__gte=since)
        if self.instance.cashflow_movement_id:
            receipts = receipts | CashflowMovement.objects.filter(
                organization=organization, pk=self.instance.cashflow_movement_id
            )
        field = self.fields["cashflow_movement"]
        field.queryset = receipts.order_by("-transaction_date")
        field.label = "Cashflow receipt"
        field.label_from_instance = lambda row: (
            f"{row.transaction_date:%Y-%m-%d} · {row.currency} {row.amount} · {row.counterparty_name or row.category}"
        )


def product_label(product):
    """What the line's product box shows and accepts: the SKU, then the title."""
    return f"{product.sku} · {product.title}"


def read_lines(post, organization):
    """Purchase lines from the form, resolved to this organization's products.

    Returns (lines, errors). Each line is a dict with id, product, quantity, unit_cost,
    supplier_sku and the raw text, so a form with errors shows what was typed.
    """
    rows = zip(*(post.getlist(name) for name in LINE_FIELDS))
    lines, errors = [], []
    typed = [row for row in rows if any(part.strip() for part in row[1:])]
    skus = {_sku(row[1]) for row in typed}
    products = {
        product.sku: product
        for product in MasterProduct.objects.filter(organization=organization, sku__in=skus)
    }
    for number, (line_id, text, quantity, unit_cost, supplier_sku) in enumerate(typed, start=1):
        line = {
            "id": line_id.strip(),
            "product_text": text.strip(),
            "quantity_text": quantity.strip(),
            "unit_cost_text": unit_cost.strip(),
            "supplier_sku": supplier_sku.strip()[:64],
            "errors": [],
        }
        line["product"] = products.get(_sku(text))
        if line["product"] is None:
            line["errors"].append("Pick a product from the catalog by its SKU.")
        try:
            line["quantity"] = int(quantity)
            if not (1 <= line["quantity"] <= 2147483647):
                raise ValueError
        except ValueError:
            line["errors"].append("Quantity must be a whole number of 1 or more.")
        try:
            line["unit_cost"] = Decimal(unit_cost.replace(",", ".")).quantize(Decimal("0.01"))
            if not (0 <= line["unit_cost"] < MAX_COST):
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            line["errors"].append("Unit cost must be a number of 0 or more.")
        if line["errors"]:
            errors.append(f"Line {number}: " + " ".join(line["errors"]))
        lines.append(line)
    if not typed:
        errors.append("Add at least one line.")
    return lines, errors


def _sku(text):
    return (text or "").split("·")[0].strip().upper()
