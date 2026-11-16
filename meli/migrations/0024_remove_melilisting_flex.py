from django.db import migrations


class Migration(migrations.Migration):
    """Flex is a delivery method, not where the units are: the listings page filters by Full instead."""

    dependencies = [
        ('meli', '0023_melilisting_flex'),
    ]

    operations = [
        migrations.RemoveField(model_name='melilisting', name='flex'),
    ]
