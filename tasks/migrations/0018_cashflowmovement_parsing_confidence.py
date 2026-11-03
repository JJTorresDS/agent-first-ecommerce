import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0017_cashflowmovement_document_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='cashflowmovement',
            name='parsing_confidence',
            field=models.DecimalField(
                blank=True,
                decimal_places=3,
                help_text='0–1: probability that the parsed fields are correct (Jev per-field checks, capped by deterministic checks).',
                max_digits=4,
                null=True,
                validators=[
                    django.core.validators.MinValueValidator(0),
                    django.core.validators.MaxValueValidator(1),
                ],
            ),
        ),
    ]
