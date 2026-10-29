import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def backfill_default_organizations(apps, schema_editor):
    User = apps.get_model("auth", "User")
    Organization = apps.get_model("accounts", "Organization")
    Membership = apps.get_model("accounts", "Membership")
    for user in User.objects.all():
        if Membership.objects.filter(user_id=user.pk).exists():
            continue
        organization = Organization.objects.create(id=uuid.uuid4(), name="default")
        Membership.objects.create(
            user_id=user.pk,
            organization_id=organization.id,
            role="owner",
        )


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0002_app_access"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Organization",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("name", models.CharField(max_length=200)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name="Membership",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "role",
                    models.CharField(
                        choices=[
                            ("owner", "Owner"),
                            ("operator", "Operator"),
                            ("viewer", "Viewer"),
                        ],
                        default="owner",
                        max_length=32,
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="memberships",
                        to="accounts.organization",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="memberships",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="membership",
            constraint=models.UniqueConstraint(
                fields=("user", "organization"),
                name="uniq_user_organization",
            ),
        ),
        migrations.RunPython(
            backfill_default_organizations,
            migrations.RunPython.noop,
        ),
    ]
