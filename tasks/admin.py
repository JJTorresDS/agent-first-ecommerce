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
        "date",
        "amount",
        "currency",
        "category",
        "counterparty_name",
        "email_address",
        "white",
        "doc_moved_to_date_folder",
    )
    search_fields = (
        "email_address",
        "category",
        "counterparty_name",
        "description",
        "source_file_id",
    )
    readonly_fields = ("id", "created_at", "updated_at")

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        email = email_from_account(request.user)
        if email:
            initial["email_address"] = email
        return initial

    def save_model(self, request, obj, form, change):
        if not obj.email_address:
            obj.email_address = email_from_account(request.user)
        super().save_model(request, obj, form, change)
