from datetime import timedelta

from django.db import models
from django.utils import timezone

from .tokens import decrypt_secret, encrypt_secret


class MercadoLibreAccount(models.Model):
    """One Mercado Libre seller connected to an organization."""

    organization = models.OneToOneField(
        "accounts.Organization",
        on_delete=models.CASCADE,
        related_name="mercadolibre_account",
    )
    meli_user_id = models.CharField(max_length=64, unique=True)
    access_token = models.TextField()
    refresh_token = models.TextField(blank=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "mercadolibre_account"

    def __str__(self):
        return self.meli_user_id

    def store_token_response(self, payload):
        from .client import expires_at_from

        self.access_token = encrypt_secret(payload.get("access_token") or "")
        refresh = payload.get("refresh_token")
        if refresh:
            self.refresh_token = encrypt_secret(refresh)
        user_id = payload.get("user_id")
        if user_id is not None:
            self.meli_user_id = str(user_id)
        self.expires_at = expires_at_from(payload.get("expires_in"))

    def fresh_access_token(self):
        if self.expires_at > timezone.now() + timedelta(minutes=5):
            return decrypt_secret(self.access_token)
        from .client import refresh_access_token

        payload = refresh_access_token(decrypt_secret(self.refresh_token))
        self.store_token_response(payload)
        self.save(
            update_fields=["access_token", "refresh_token", "expires_at", "updated_at"]
        )
        return decrypt_secret(self.access_token)


class MercadoLibreNotification(models.Model):
    """A Mercado Libre webhook delivery. Organization is set when the seller is connected."""

    organization = models.ForeignKey(
        "accounts.Organization",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="mercadolibre_notifications",
    )
    notification_id = models.CharField(max_length=128, blank=True)
    topic = models.CharField(max_length=64, blank=True)
    resource = models.CharField(max_length=512, blank=True)
    user_id = models.CharField(max_length=64, blank=True)
    application_id = models.CharField(max_length=64, blank=True)
    attempts = models.PositiveIntegerField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    payload = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "mercadolibre_notification"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["notification_id"],
                condition=~models.Q(notification_id=""),
                name="uniq_meli_notification_id",
            ),
        ]

    def __str__(self):
        return f"{self.topic} {self.resource}".strip()


class SellerProfile(models.Model):
    """The Mercado Libre seller connected to an organization."""

    organization = models.OneToOneField(
        "accounts.Organization",
        on_delete=models.CASCADE,
        related_name="seller_profile",
    )
    meli_user_id = models.CharField(max_length=64, blank=True)
    nickname = models.CharField(max_length=128, blank=True)
    first_name = models.CharField(max_length=200, blank=True)
    last_name = models.CharField(max_length=200, blank=True)
    email = models.CharField(max_length=254, blank=True)
    site_id = models.CharField(max_length=16, blank=True)
    country_id = models.CharField(max_length=8, blank=True)
    permalink = models.CharField(max_length=512, blank=True)
    payload = models.JSONField(default=dict)
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "seller_profile"
        constraints = [
            models.UniqueConstraint(
                fields=["meli_user_id"],
                condition=models.Q(deleted_at__isnull=True)
                & ~models.Q(meli_user_id=""),
                name="uniq_seller_profile_live_user",
            ),
        ]

    def __str__(self):
        return self.nickname or self.meli_user_id

    def display_name(self):
        return " ".join(part for part in (self.first_name, self.last_name) if part)

    def profile_url(self):
        if self.permalink.startswith(("http://", "https://")):
            return self.permalink
        return ""

    def store_user_profile(self, payload):
        self.meli_user_id = _text(payload.get("id"), 64)
        self.nickname = _text(payload.get("nickname"), 128)
        self.first_name = _text(payload.get("first_name"), 200)
        self.last_name = _text(payload.get("last_name"), 200)
        self.email = _text(payload.get("email"), 254)
        self.site_id = _text(payload.get("site_id"), 16)
        self.country_id = _text(payload.get("country_id"), 8)
        self.permalink = _text(payload.get("permalink"), 512)
        self.payload = payload
        self.deleted_at = None


def _text(value, limit):
    if value is None:
        return ""
    return str(value)[:limit]
