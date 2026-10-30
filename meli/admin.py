from django.contrib import admin

from .models import BUENOS_AIRES, MercadoLibreAccount, MercadoLibreNotification


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
