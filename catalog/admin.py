from django.contrib import admin

from purchasing.models import PurchaseItem

from .models import MasterProduct


class PurchaseLineInline(admin.TabularInline):
    model = PurchaseItem
    fields = ("purchase", "quantity", "unit_cost", "supplier_sku")
    readonly_fields = fields
    extra = 0
    can_delete = False
    verbose_name_plural = "Purchases"

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(MasterProduct)
class MasterProductAdmin(admin.ModelAdmin):
    list_display = ("sku", "title", "brand", "family", "status", "list_price", "organization")
    list_filter = ("status", "condition", "brand", "organization")
    search_fields = ("sku", "title", "gtin", "family", "brand")
    inlines = [PurchaseLineInline]
    fieldsets = (
        (None, {"fields": ("organization", "sku", "title", "family", "status", "image_url")}),
        ("Listing", {"fields": ("brand", "model", "gtin", "category", "condition", "description")}),
        ("Price", {"fields": ("list_price", "currency")}),
        ("Package", {"fields": ("weight_g", "length_cm", "width_cm", "height_cm")}),
        ("Secondary attributes", {"fields": ("attributes",)}),
    )
