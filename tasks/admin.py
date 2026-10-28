from django.contrib import admin

from .models import Task


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
