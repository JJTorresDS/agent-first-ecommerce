import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0018_cashflowmovement_parsing_confidence'),
    ]

    operations = [
        migrations.RenameField(
            model_name='cashflowmovement',
            old_name='parsing_confidence',
            new_name='amount_parsing_confidence',
        ),
        migrations.AlterField(
            model_name='cashflowmovement',
            name='amount_parsing_confidence',
            field=models.DecimalField(
                blank=True,
                decimal_places=3,
                help_text='0–1: probability that the parsed amount is correct (Jev per-field checks, capped by deterministic checks).',
                max_digits=4,
                null=True,
                validators=[
                    django.core.validators.MinValueValidator(0),
                    django.core.validators.MaxValueValidator(1),
                ],
            ),
        ),
        migrations.AddField(
            model_name='cashflowmovement',
            name='parsing_observations',
            field=models.TextField(blank=True, default=''),
            preserve_default=False,
        ),
    ]
