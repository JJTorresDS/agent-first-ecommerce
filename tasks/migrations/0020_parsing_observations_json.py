import json

from django.db import migrations, models


def blank_to_null(apps, schema_editor):
    CashflowMovement = apps.get_model("tasks", "CashflowMovement")
    for movement in CashflowMovement.objects.exclude(parsing_observations__isnull=True).iterator():
        try:
            json.loads(movement.parsing_observations)
        except (TypeError, ValueError):
            movement.parsing_observations = None
            movement.save(update_fields=["parsing_observations"])


class Migration(migrations.Migration):

    dependencies = [
        ("tasks", "0019_amount_parsing_confidence_and_observations"),
    ]

    operations = [
        migrations.AlterField(
            model_name="cashflowmovement",
            name="parsing_observations",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.RunPython(blank_to_null, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="cashflowmovement",
            name="parsing_observations",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
