from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tasks", "0010_cashflowmovement_sub_category"),
    ]

    operations = [
        migrations.AlterField(
            model_name="cashflowmovement",
            name="sub_category",
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
    ]
