import hashlib
import json

from django.conf import settings
from django.db import models

from accounts.models import TenantModel


class ErrorReport(TenantModel):
    """A problem a user reported from the page where it showed, with what's needed to debug it.

    details is what Vimple sent and what the other side answered; context is the state around it.
    Neither ever holds a token. A repeat of an open report adds to it (occurrences) instead of a new row.
    Read them in the admin or with manage.py error_reports.
    """

    class Source(models.TextChoices):
        LISTING_DRAFT = "listing_draft", "Mercado Libre listing draft"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        RESOLVED = "resolved", "Resolved"

    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    source = models.CharField(max_length=32, choices=Source.choices)
    object_id = models.CharField(max_length=64, help_text="The reported row's id, kept if the row is deleted.")
    title = models.CharField(max_length=500, blank=True, help_text="The message the user saw.")
    call = models.CharField(max_length=200, blank=True, help_text="The call that failed, e.g. POST /items/validate.")
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    error_code = models.CharField(max_length=100, blank=True)
    cause_codes = models.JSONField(default=list, blank=True)
    details = models.JSONField(default=dict, blank=True, help_text="What was sent and the full answer. Never a token.")
    context = models.JSONField(default=dict, blank=True, help_text="The state around it: settings, ids, app version.")
    note = models.TextField(blank=True, max_length=2000, help_text="What the user was doing.")
    fingerprint = models.CharField(max_length=40, db_index=True)
    occurrences = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    resolution = models.TextField(blank=True, help_text="What was wrong and how it was fixed.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "support_error_report"
        ordering = ["-updated_at"]
        indexes = [models.Index(fields=["organization", "status"])]

    def save(self, *args, **kwargs):
        from django.utils import timezone

        if self.status == self.Status.RESOLVED and self.resolved_at is None:
            self.resolved_at = timezone.now()
        elif self.status == self.Status.OPEN:
            self.resolved_at = None
        super().save(*args, **kwargs)

    def __str__(self):
        return f"#{self.pk} {self.get_source_display()} {self.object_id}: {self.title[:60]}"

    @staticmethod
    def fingerprint_of(source, object_id, call, error_code, cause_codes):
        raw = json.dumps([source, str(object_id), call, error_code, sorted(cause_codes)])
        return hashlib.sha1(raw.encode()).hexdigest()

    def as_dict(self):
        """Everything about the report, for the command a coding agent reads."""
        return {
            "id": self.pk,
            "organization_id": str(self.organization_id),
            "reported_by": self.reported_by.get_username() if self.reported_by_id else None,
            "source": self.source,
            "object_id": self.object_id,
            "status": self.status,
            "occurrences": self.occurrences,
            "title": self.title,
            "call": self.call,
            "http_status": self.http_status,
            "error_code": self.error_code,
            "cause_codes": self.cause_codes,
            "note": self.note,
            "context": self.context,
            "details": self.details,
            "resolution": self.resolution,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
        }
