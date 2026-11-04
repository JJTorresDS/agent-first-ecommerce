import json
import re
import uuid
from urllib.parse import urlparse

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from accounts.models import TenantModel

_DRIVE_ID = re.compile(r"(?:/d/|/folders/|[?&]id=)([a-zA-Z0-9_-]+)")
_FILE_ID = re.compile(r"^[a-zA-Z0-9_-]+$")
_GOOGLE_HOSTS = {"docs.google.com", "drive.google.com"}


def drive_file_id(url):
    """Return a Drive file id when url is a Google Docs or Drive link."""
    if not url:
        return ""
    host = urlparse(url).netloc.lower()
    if host not in _GOOGLE_HOSTS:
        return ""
    match = _DRIVE_ID.search(url)
    return match.group(1) if match else ""


def drive_file_url(file_id):
    """Open link for a Drive file id. Empty when the id is missing or unsafe."""
    if not file_id or not _FILE_ID.fullmatch(file_id):
        return ""
    return f"https://drive.google.com/file/d/{file_id}/view"


class Task(TenantModel):
    class Priority(models.TextChoices):
        LOW = "1-Low: can be pushed to next month", "1-Low: can be pushed to next month"
        MEDIUM = "2-Medium: Do this month", "2-Medium: Do this month"
        HIGH = "3-High: Do this week", "3-High: Do this week"

    class Status(models.TextChoices):
        TODO = "To do", "To do"
        IN_PROGRESS = "In progress", "In progress"
        BLOCKED = "Blocked", "Blocked"
        DONE = "Done", "Done"

    class Effort(models.TextChoices):
        QUICK = "Quick: under an hour", "Quick: under an hour"
        SHORT = "Short: half a day", "Short: half a day"
        MEDIUM = "Medium: about a day", "Medium: about a day"
        LARGE = "Large: a few days", "Large: a few days"
        EPIC = "Epic: a week or more", "Epic: a week or more"

    timestamp = models.DateTimeField(
        default=timezone.now,
        help_text="Form submission time",
    )
    email_address = models.EmailField(
        help_text="Submitter (human or agent)",
    )
    task_title = models.CharField(max_length=500)
    priority = models.CharField(
        max_length=200,
        blank=True,
        choices=Priority.choices,
    )
    description = models.TextField(blank=True)
    task_deepdive_link = models.URLField(
        max_length=2048,
        blank=True,
        help_text="Google Docs/Drive URL",
    )
    ticket_id = models.CharField(
        max_length=128,
        blank=True,
        help_text="Drive file ID taken from the deepdive link",
    )
    effort = models.CharField(
        max_length=100,
        blank=True,
        choices=Effort.choices,
    )
    status = models.CharField(
        max_length=100,
        blank=True,
        choices=Status.choices,
    )
    assignee = models.CharField(max_length=200, blank=True)

    class Meta:
        db_table = "tasks"
        ordering = ["-timestamp"]

    def __str__(self):
        return self.task_title

    def save(self, *args, **kwargs):
        if not self.ticket_id and self.task_deepdive_link:
            self.ticket_id = drive_file_id(self.task_deepdive_link)
        super().save(*args, **kwargs)


def new_cashflow_id():
    return uuid.uuid4().hex


def email_from_account(user):
    """Address stored on the user. Profile points at that same user."""
    if not getattr(user, "is_authenticated", False):
        return ""
    getattr(user, "profile", None)
    return user.email or ""


class DocumentType(models.TextChoices):
    RECEIPT = "receipt", "Receipt"
    INVOICE = "invoice", "Invoice"


class CashflowMovement(TenantModel):
    id = models.CharField(
        primary_key=True,
        max_length=32,
        default=new_cashflow_id,
        editable=False,
    )
    uploader_email = models.EmailField()
    transaction_date = models.DateField()
    category = models.CharField(max_length=100)
    sub_category = models.CharField(max_length=100, null=True, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=8)
    exchange_rate_at_purchase = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        null=True,
        blank=True,
    )
    counterparty_name = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True)
    source_file_id = models.CharField(max_length=128, blank=True)
    receipt_id = models.CharField(
        max_length=128,
        blank=True,
        help_text="Invoice or ticket id from the document, when one is present.",
    )
    document_type = models.CharField(
        max_length=16,
        choices=DocumentType.choices,
        blank=True,
    )
    amount_parsing_confidence = models.DecimalField(
        max_digits=4,
        decimal_places=3,
        null=True,
        blank=True,  # rows created before this field have no score
        validators=[MinValueValidator(0), MaxValueValidator(1)],
        help_text="0–1: probability that the parsed amount is correct (Jev per-field checks, capped by deterministic checks).",
    )
    parsing_observations = models.JSONField(null=True, blank=True)
    white = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "cashflow_movement"
        ordering = ["-transaction_date", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "source_file_id"],
                condition=~models.Q(source_file_id=""),
                name="uniq_cashflow_org_source_file_id",
            ),
        ]

    def __str__(self):
        return f"{self.date} {self.amount} {self.currency}"

    def source_file_url(self):
        return drive_file_url(self.source_file_id)

    def parsing_observations_text(self):
        if self.parsing_observations is None:
            return ""
        return json.dumps(self.parsing_observations, ensure_ascii=False)


# Receipt folders the n8n parser reads, in the order the settings page shows them.
FOLDER_SLOTS = [
    ("receipts_folder", "Receipts inbox"),
    ("cost.sueldos", "Cost · Salaries"),
    ("cost.servicios", "Cost · Services"),
    ("cost.insumos", "Cost · Supplies"),
    ("cost.productos", "Cost · Products"),
    ("profit.productos", "Income · Products"),
    ("profit.servicios", "Income · Services"),
]


def folder_id(value):
    """A Drive folder id from an id or a Drive link. Empty when it is neither."""
    value = (value or "").strip()
    if not value:
        return ""
    if _FILE_ID.fullmatch(value):
        return value
    return drive_file_id(value)


class CashflowParserSettings(TenantModel):
    """The receipt folders one organization sends to the receipt parser."""

    class Status(models.TextChoices):
        NONE = "", "Never run"
        RUNNING = "running", "Running"
        OK = "ok", "Started"
        FAILED = "failed", "Failed"

    folders = models.JSONField(default=dict, blank=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_run_status = models.CharField(
        max_length=16, choices=Status.choices, default="", blank=True
    )
    last_run_error = models.CharField(max_length=200, blank=True)
    last_run_by = models.EmailField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "cashflow_parser_settings"
        constraints = [
            models.UniqueConstraint(
                fields=["organization"],
                name="uniq_cashflow_parser_settings_org",
            ),
        ]

    def __str__(self):
        return f"Parser settings for {self.organization_id}"

    def folder(self, slot):
        entry = self.folders.get(slot) if isinstance(self.folders, dict) else None
        if not isinstance(entry, dict):
            return {"id": "", "name": ""}
        return {"id": str(entry.get("id") or ""), "name": str(entry.get("name") or "")}

    def missing_slots(self):
        return [label for slot, label in FOLDER_SLOTS if not self.folder(slot)["id"]]
