import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0003_organization"),
    ]

    operations = [
        migrations.AddField(
            model_name="appaccess",
            name="organization",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="accounts_appaccess_set",
                to="accounts.organization",
            ),
        ),
        migrations.AddField(
            model_name="profile",
            name="organization",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="accounts_profile_set",
                to="accounts.organization",
            ),
        ),
    ]
