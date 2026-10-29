import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0003_organization"),
        ("tasks", "0012_remove_cashflowmovement_doc_moved_to_date_folder"),
    ]

    operations = [
        migrations.AddField(
            model_name="task",
            name="organization",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="tasks_task_set",
                to="accounts.organization",
            ),
        ),
        migrations.AddField(
            model_name="cashflowmovement",
            name="organization",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="tasks_cashflowmovement_set",
                to="accounts.organization",
            ),
        ),
    ]
