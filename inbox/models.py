import uuid
from zoneinfo import ZoneInfo

from django.db import models

from accounts.models import TenantModel

BUENOS_AIRES = ZoneInfo("America/Argentina/Buenos_Aires")


class Channel(models.TextChoices):
    MELI_ITEM_QUESTIONS = "meli-itemquestions", "meli-itemquestions"


class Conversation(TenantModel):
    """One source thread, such as a single Mercado Libre question."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    channel = models.CharField(max_length=32)
    external_id = models.CharField(max_length=64)
    title = models.CharField(max_length=200, blank=True)
    preview = models.CharField(max_length=200, blank=True)
    link_url = models.CharField(max_length=512, blank=True)
    needs_reply = models.BooleanField(default=False)
    last_message_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-last_message_at", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "channel", "external_id"],
                name="inbox_conversation_source",
            )
        ]

    def __str__(self):
        return f"{self.channel}:{self.external_id}"

    def safe_link(self):
        if self.link_url.startswith(("http://", "https://")):
            return self.link_url
        return ""


class Message(TenantModel):
    """One bubble in a conversation."""

    class Direction(models.TextChoices):
        INBOUND = "inbound", "Inbound"
        OUTBOUND = "outbound", "Outbound"

    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name="messages",
    )
    direction = models.CharField(max_length=16)
    body = models.TextField(blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    external_id = models.CharField(max_length=80)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sent_at", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "external_id"],
                name="inbox_message_source",
            )
        ]

    def __str__(self):
        return self.external_id

    def sent_label(self):
        if self.sent_at is None:
            return ""
        return self.sent_at.astimezone(BUENOS_AIRES).strftime("%Y-%m-%d %H:%M")
