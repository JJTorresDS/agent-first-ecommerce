from django.contrib import admin

from .models import ErrorReport


@admin.register(ErrorReport)
class ErrorReportAdmin(admin.ModelAdmin):
    list_display = ("id", "organization", "source", "object_id", "title", "occurrences", "status", "updated_at")
    list_filter = ("status", "source", "organization")
    search_fields = ("title", "object_id", "error_code", "note")
    readonly_fields = (
        "organization", "reported_by", "source", "object_id", "title", "call", "http_status", "error_code",
        "cause_codes", "details", "context", "note", "fingerprint", "occurrences", "created_at", "updated_at",
    )
    fields = readonly_fields[:13] + ("status", "resolution", "resolved_at") + readonly_fields[13:]
