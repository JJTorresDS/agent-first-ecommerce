import re
from urllib.parse import urlparse

from django.db import models
from django.utils import timezone

_DRIVE_ID = re.compile(r"(?:/d/|/folders/|[?&]id=)([a-zA-Z0-9_-]+)")
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


class Task(models.Model):
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
        help_text="e.g. '1-Low: can be pushed to next month', '3-High: Do this week'",
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
    effort = models.CharField(max_length=100, blank=True)
    status = models.CharField(
        max_length=100,
        blank=True,
        help_text="e.g. 'Done'",
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
