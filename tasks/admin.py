from django.contrib import admin

from .models import CashflowMovement, Task, email_from_account


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = (
        "timestamp",
        "task_title",
        "priority",
        "status",
        "assignee",
        "email_address",
    )
    list_filter = ("priority", "status", "effort")
    search_fields = (
        "task_title",
        "email_address",
        "assignee",
        "description",
        "ticket_id",
    )


@admin.register(CashflowMovement)
class CashflowMovementAdmin(admin.ModelAdmin):
    list_display = (
        "transaction_date",
        "amount",
        "currency",
        "category",
        "counterparty_name",
        "uploader_email",
        "white",
        "sub_category",
        "document_type",
    )
    list_filter = ("document_type",)
    search_fields = (
        "uploader_email",
        "category",
        "counterparty_name",
        "description",
        "source_file_id",
        "receipt_id",
    )
    readonly_fields = ("id", "created_at", "updated_at")

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        email = email_from_account(request.user)
        if email:
            initial["uploader_email"] = email
        return initial

    def save_model(self, request, obj, form, change):
        if not obj.email_address:
            obj.uploader_email = email_from_account(request.user)
        super().save_model(request, obj, form, change)
