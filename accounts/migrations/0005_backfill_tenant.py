import uuid

import django.db.models.deletion
from django.db import migrations, models


def backfill_organizations(apps, schema_editor):
    User = apps.get_model("auth", "User")
    Organization = apps.get_model("accounts", "Organization")
    Membership = apps.get_model("accounts", "Membership")
    Profile = apps.get_model("accounts", "Profile")
    AppAccess = apps.get_model("accounts", "AppAccess")
    Task = apps.get_model("tasks", "Task")
    CashflowMovement = apps.get_model("tasks", "CashflowMovement")

    def earliest():
        organization = Organization.objects.order_by("created_at", "id").first()
        if organization is not None:
            return organization.id
        created = Organization.objects.create(id=uuid.uuid4(), name="default")
        return created.id

    def for_user(user_id):
        membership = (
            Membership.objects.filter(user_id=user_id).order_by("pk").first()
        )
        if membership is not None:
            return membership.organization_id
        return earliest()

    def for_email(email):
        if email:
            user = User.objects.filter(email__iexact=email).order_by("pk").first()
            if user is not None:
                return for_user(user.pk)
        return earliest()

    for profile in Profile.objects.filter(organization_id__isnull=True):
        profile.organization_id = for_user(profile.user_id)
        profile.save(update_fields=["organization_id"])

    for access in AppAccess.objects.filter(organization_id__isnull=True):
        access.organization_id = for_user(access.user_id)
        access.save(update_fields=["organization_id"])

    for task in Task.objects.filter(organization_id__isnull=True):
        task.organization_id = for_email(task.email_address)
        task.save(update_fields=["organization_id"])

    for movement in CashflowMovement.objects.filter(organization_id__isnull=True):
        movement.organization_id = for_email(movement.uploader_email)
        movement.save(update_fields=["organization_id"])


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0004_profile_appaccess_organization"),
        ("tasks", "0013_task_cashflow_organization"),
    ]

    operations = [
        migrations.RunPython(backfill_organizations, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="appaccess",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="accounts_appaccess_set",
                to="accounts.organization",
            ),
        ),
        migrations.AlterField(
            model_name="profile",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="accounts_profile_set",
                to="accounts.organization",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="appaccess",
            name="uniq_user_app_access",
        ),
        migrations.AddConstraint(
            model_name="appaccess",
            constraint=models.UniqueConstraint(
                fields=("user", "app", "organization"),
                name="uniq_user_app_organization",
            ),
        ),
    ]
