from django.contrib import admin

from .models import MercadoLibreNotification


@admin.register(MercadoLibreNotification)
class MercadoLibreNotificationAdmin(admin.ModelAdmin):
    list_display = ("topic", "resource", "user_id", "created_at")
    search_fields = ("topic", "resource", "user_id", "notification_id")
    readonly_fields = ("created_at",)
