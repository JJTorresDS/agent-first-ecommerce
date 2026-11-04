from django.contrib import admin

from purchasing.models import PurchaseItem

from .models import Brand, Category, MasterProduct, ProductModel


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "organization")
    list_filter = ("organization",)
    search_fields = ("code", "name")


@admin.register(Brand)
class BrandAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "organization")
    list_filter = ("organization",)
    search_fields = ("code", "name")


class VariantInline(admin.TabularInline):
    model = MasterProduct
    fields = ("variant", "sku", "title", "image_url", "gtin", "list_price", "currency", "status")
    readonly_fields = ("sku",)
    extra = 1
    show_change_link = True
    verbose_name = "variant"
    verbose_name_plural = "variants"


@admin.register(ProductModel)
class ProductModelAdmin(admin.ModelAdmin):
    list_display = ("family", "name", "category", "brand", "organization")
    list_filter = ("organization", "category", "brand")
    search_fields = ("code", "name", "brand__code", "brand__name", "category__code", "category__name")
    autocomplete_fields = ("category", "brand")
    readonly_fields = ("family",)
    fields = ("organization", "category", "brand", "code", "family", "name", "condition", "description")
    inlines = [VariantInline]

    @admin.display(description="Family")
    def family(self, obj):
        return obj.family_code() if obj.pk else "Shown after saving"

    def save_formset(self, request, form, formset, change):
        # Variants take the model's organization.
        for variant in formset.save(commit=False):
            variant.organization_id = form.instance.organization_id
            variant.save()
        for variant in formset.deleted_objects:
            variant.delete()
        formset.save_m2m()


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
    list_display = ("sku", "title", "brand", "category", "status", "list_price", "organization")
    list_filter = ("status", "organization", "product_model__brand", "product_model__category")
    search_fields = (
        "sku",
        "title",
        "gtin",
        "product_model__name",
        "product_model__brand__name",
        "product_model__category__name",
    )
    list_select_related = ("product_model__brand", "product_model__category")
    autocomplete_fields = ("product_model",)
    inlines = [PurchaseLineInline]
    fieldsets = (
        (None, {"fields": ("organization", "product_model", "variant", "sku", "title", "status", "image_url")}),
        ("Listing", {"fields": ("gtin",)}),
        ("Price", {"fields": ("list_price", "currency")}),
        ("Package", {"fields": ("weight_g", "length_cm", "width_cm", "height_cm")}),
        ("Secondary attributes", {"fields": ("attributes",)}),
    )

    def get_readonly_fields(self, request, obj=None):
        # Model and variant make the SKU, so they are fixed once saved.
        if obj is None:
            return ("sku",)
        return ("sku", "product_model", "variant")

    @admin.display(ordering="product_model__brand__name")
    def brand(self, obj):
        return obj.product_model.brand

    @admin.display(ordering="product_model__category__name")
    def category(self, obj):
        return obj.product_model.category
