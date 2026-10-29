import uuid

from django.conf import settings
from django.db import models


class Organization(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class TenantModel(models.Model):
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="%(app_label)s_%(class)s_set",
    )

    class Meta:
        abstract = True


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


class AppAccess(TenantModel):
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
                fields=["user", "app", "organization"],
                name="uniq_user_app_organization",
            ),
        ]

    def __str__(self):
        return f"{self.user} {self.app}"


class Profile(TenantModel):
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


def membership_for(user):
    if not getattr(user, "is_authenticated", False):
        return None
    return (
        Membership.objects.select_related("organization")
        .filter(user=user)
        .order_by("pk")
        .first()
    )


def user_can_open(user, app):
    if not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser:
        return True
    membership = membership_for(user)
    if membership is None:
        return False
    return AppAccess.objects.filter(
        user=user,
        app=app,
        organization=membership.organization,
    ).exists()


def grant_access_task(user, organization):
    from django.contrib.auth import get_user_model

    from tasks.models import Task

    admin = get_user_model().objects.filter(is_superuser=True).order_by("pk").first()
    who = user.email or user.get_username()
    return Task.objects.create(
        organization=organization,
        email_address=who,
        task_title=f"Grant access to {who}",
        assignee=admin.email if admin and admin.email else "",
        status=Task.Status.TODO,
    )


def ensure_profile(user, organization):
    profile, _ = Profile.objects.get_or_create(
        user=user,
        defaults={"organization": organization},
    )
    if profile.organization_id != organization.id:
        profile.organization = organization
        profile.save(update_fields=["organization", "updated_at"])
    return profile


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
    ensure_profile(user, organization)
    grant_access_task(user, organization)
    return organization


def create_owned_organization(user, name):
    organization = Organization.objects.create(name=name)
    Membership.objects.create(
        user=user,
        organization=organization,
        role=Membership.Role.OWNER,
    )
    ensure_profile(user, organization)
    _copy_google_profile(user)
    grant_access_task(user, organization)
    return organization


def _copy_google_profile(user):
    from allauth.socialaccount.models import SocialAccount

    account = SocialAccount.objects.filter(user=user, provider="google").first()
    if account is None:
        return
    extra = account.extra_data or {}
    profile = user.profile
    name = (extra.get("name") or "").strip()
    picture = (extra.get("picture") or "").strip()
    if name:
        profile.display_name = name
    if picture:
        profile.avatar_url = picture
    if name or picture:
        profile.save(update_fields=["display_name", "avatar_url", "updated_at"])
