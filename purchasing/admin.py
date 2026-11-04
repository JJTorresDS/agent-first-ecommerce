from django.contrib import admin

from .models import Purchase, PurchaseItem, Supplier


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ("name", "tax_id", "email", "phone", "organization")
    list_filter = ("organization",)
    search_fields = ("name", "tax_id", "email")


class PurchaseItemInline(admin.TabularInline):
    model = PurchaseItem
    fields = ("product", "quantity", "unit_cost", "supplier_sku")
    autocomplete_fields = ("product",)
    extra = 1


@admin.register(Purchase)
class PurchaseAdmin(admin.ModelAdmin):
    list_display = ("purchased_on", "supplier", "invoice_number", "currency", "total", "organization")
    list_filter = ("organization", "currency")
    search_fields = ("invoice_number", "supplier__name")
    autocomplete_fields = ("supplier",)
    raw_id_fields = ("cashflow_movement",)
    inlines = [PurchaseItemInline]

    def save_formset(self, request, form, formset, change):
        # Lines take the purchase's organization.
        for line in formset.save(commit=False):
            line.organization_id = form.instance.organization_id
            line.save()
        for line in formset.deleted_objects:
            line.delete()
        formset.save_m2m()
