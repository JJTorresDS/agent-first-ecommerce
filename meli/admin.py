from django.contrib import admin

from .models import BUENOS_AIRES, ListingChange, MercadoLibreAccount, MercadoLibreNotification


@admin.register(MercadoLibreAccount)
class MercadoLibreAccountAdmin(admin.ModelAdmin):
    list_display = ("organization", "meli_user_id", "expires_at")
    fields = ("organization", "meli_user_id", "expires_at", "created_at", "updated_at")
    readonly_fields = ("created_at", "updated_at")


@admin.register(MercadoLibreNotification)
class MercadoLibreNotificationAdmin(admin.ModelAdmin):
    list_display = ("topic", "resource", "user_id", "organization", "buenos_aires_time")
    search_fields = ("topic", "resource", "user_id", "notification_id")
    readonly_fields = ("created_at", "buenos_aires_at")

    @admin.display(description="Buenos Aires", ordering="buenos_aires_at")
    def buenos_aires_time(self, obj):
        if obj.buenos_aires_at is None:
            return ""
        return obj.buenos_aires_at.astimezone(BUENOS_AIRES).strftime("%Y-%m-%d %H:%M:%S")


@admin.register(ListingChange)
class ListingChangeAdmin(admin.ModelAdmin):
    list_display = ("item_id", "title", "new_value", "reason", "user", "state", "organization", "created_at")
    list_filter = ("state", "reason", "new_value")
    search_fields = ("item_id", "title", "note")
    readonly_fields = [field.name for field in ListingChange._meta.fields]


from .models import MeliListingProspect  # noqa: E402


@admin.register(MeliListingProspect)
class MeliListingProspectAdmin(admin.ModelAdmin):
    list_display = ("external_id", "title", "status", "seller", "product", "organization", "created_at")
    list_filter = ("status",)
    search_fields = ("external_id", "title", "seller")
    raw_id_fields = ("product", "draft")
