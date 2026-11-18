import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0008_catalog_purchasing_access'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='OrganizationApiKey',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('key_hash', models.CharField(max_length=64, unique=True)),
                ('key_encrypted', models.TextField()),
                ('last4', models.CharField(max_length=4)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('last_used_at', models.DateTimeField(blank=True, null=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('organization', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='api_key', to='accounts.organization')),
            ],
            options={
                'db_table': 'accounts_organization_api_key',
            },
        ),
    ]
