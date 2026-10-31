from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("inbox", "0001_initial"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="conversation",
            options={"ordering": ["-needs_reply", "-last_message_at", "-created_at"]},
        ),
    ]
