import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0005_backfill_tenant"),
        ("tasks", "0013_task_cashflow_organization"),
    ]

    operations = [
        migrations.AlterField(
            model_name="task",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="tasks_task_set",
                to="accounts.organization",
            ),
        ),
        migrations.AlterField(
            model_name="cashflowmovement",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="tasks_cashflowmovement_set",
                to="accounts.organization",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="cashflowmovement",
            name="uniq_cashflow_source_file_id",
        ),
        migrations.AddConstraint(
            model_name="cashflowmovement",
            constraint=models.UniqueConstraint(
                condition=models.Q(("source_file_id", ""), _negated=True),
                fields=("organization", "source_file_id"),
                name="uniq_cashflow_org_source_file_id",
            ),
        ),
    ]
