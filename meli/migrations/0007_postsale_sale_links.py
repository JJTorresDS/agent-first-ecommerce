from django.db import migrations

CHANNEL = "meli-postsales"
SITE_DOMAINS = {
    "MLA": "com.ar",
    "MLM": "com.mx",
    "MLB": "com.br",
    "MLC": "cl",
    "MCO": "com.co",
    "MLU": "com.uy",
    "MPE": "com.pe",
}


def link_sales(apps, schema_editor):
    """Point each organization's post-sale chats at its own sale page."""
    SellerProfile = apps.get_model("meli", "SellerProfile")
    Conversation = apps.get_model("inbox", "Conversation")
    for profile in SellerProfile.objects.all():
        domain = SITE_DOMAINS.get((profile.site_id or "").upper())
        if not domain:
            continue
        conversations = Conversation.objects.filter(
            organization_id=profile.organization_id,
            channel=CHANNEL,
            link_url="",
        )
        for conversation in conversations:
            if not conversation.external_id.isdigit():
                continue
            conversation.link_url = (
                f"https://www.mercadolibre.{domain}/ventas/{conversation.external_id}/detalle"
            )
            conversation.save(update_fields=["link_url"])


class Migration(migrations.Migration):

    dependencies = [
        ("inbox", "0002_conversation_unanswered_first"),
        ("meli", "0006_project_item_questions"),
    ]

    operations = [
        migrations.RunPython(link_sales, migrations.RunPython.noop),
    ]
