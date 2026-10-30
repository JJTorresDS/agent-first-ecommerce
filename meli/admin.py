from django.contrib import admin

from .models import MercadoLibreAccount, MercadoLibreNotification


@admin.register(MercadoLibreAccount)
class MercadoLibreAccountAdmin(admin.ModelAdmin):
    list_display = ("organization", "meli_user_id", "expires_at")
    fields = ("organization", "meli_user_id", "expires_at", "created_at", "updated_at")
    readonly_fields = ("created_at", "updated_at")


@admin.register(MercadoLibreNotification)
class MercadoLibreNotificationAdmin(admin.ModelAdmin):
    list_display = ("topic", "resource", "user_id", "organization", "created_at")
    search_fields = ("topic", "resource", "user_id", "notification_id")
    readonly_fields = ("created_at",)
