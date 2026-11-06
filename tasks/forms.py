from django import forms

from .models import CashflowMovement


class CashflowMovementForm(forms.ModelForm):
    """What a person may correct on a parsed movement; the parser's own fields stay as it wrote them."""

    class Meta:
        model = CashflowMovement
        fields = [
            "transaction_date",
            "category",
            "sub_category",
            "amount",
            "currency",
            "exchange_rate_at_purchase",
            "counterparty_name",
            "description",
            "receipt_id",
            "document_type",
            "white",
        ]
        widgets = {
            "transaction_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "description": forms.Textarea(attrs={"rows": 3}),
        }
