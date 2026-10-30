from django.db import models


class MercadoLibreNotification(models.Model):
    """A Mercado Libre webhook delivery. Not tied to an organization yet."""

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
