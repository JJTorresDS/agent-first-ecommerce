from django.conf import settings
from django.db import models


class AppAccess(models.Model):
    class App(models.TextChoices):
        TASKS = "tasks", "Tasks"
        CASHFLOW = "cashflow", "Cashflow"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="app_access",
    )
    app = models.CharField(max_length=32, choices=App.choices)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "app"],
                name="uniq_user_app_access",
            ),
        ]

    def __str__(self):
        return f"{self.user} {self.app}"


def user_can_open(user, app):
    if not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser:
        return True
    return AppAccess.objects.filter(user=user, app=app).exists()


class Profile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
    )
    display_name = models.CharField(max_length=200, blank=True)
    avatar_url = models.URLField(max_length=2048, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.display_name or self.user.email or self.user.get_username()
