import uuid

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


class Organization(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class Membership(models.Model):
    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        OPERATOR = "operator", "Operator"
        VIEWER = "viewer", "Viewer"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    role = models.CharField(max_length=32, choices=Role.choices, default=Role.OWNER)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "organization"],
                name="uniq_user_organization",
            ),
        ]

    def __str__(self):
        return f"{self.user} {self.organization}"


def membership_for(user):
    if not getattr(user, "is_authenticated", False):
        return None
    return (
        Membership.objects.select_related("organization")
        .filter(user=user)
        .order_by("pk")
        .first()
    )


def assign_default_organization(user):
    """Give a user with no membership an organization named default."""
    existing = Membership.objects.filter(user=user).select_related("organization").first()
    if existing:
        return existing.organization
    organization = Organization.objects.create(name="default")
    Membership.objects.create(
        user=user,
        organization=organization,
        role=Membership.Role.OWNER,
    )
    return organization


def create_owned_organization(user, name):
    organization = Organization.objects.create(name=name)
    Membership.objects.create(
        user=user,
        organization=organization,
        role=Membership.Role.OWNER,
    )
    return organization
