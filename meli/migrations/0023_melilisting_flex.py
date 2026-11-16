from django.db import migrations, models


def backfill(apps, schema_editor):
    """Read Flex from each row's synced payload, so the filter works before the next sync."""
    MeliListing = apps.get_model("meli", "MeliListing")
    flex_ids = []
    for pk, payload in MeliListing.objects.values_list("pk", "payload").iterator():
        tags = ((payload or {}).get("shipping") or {}).get("tags") or []
        if isinstance(tags, list) and "self_service_in" in tags:
            flex_ids.append(pk)
    for start in range(0, len(flex_ids), 500):
        MeliListing.objects.filter(pk__in=flex_ids[start:start + 500]).update(flex=True)


class Migration(migrations.Migration):

    dependencies = [
        ('meli', '0022_catalogcompetition'),
    ]

    operations = [
        migrations.AddField(
            model_name='melilisting',
            name='flex',
            field=models.BooleanField(default=False, help_text='In Mercado Envíos Flex: shipping tag self_service_in, as last synced.'),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
