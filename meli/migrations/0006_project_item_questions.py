from django.db import migrations

CHANNEL = "meli-itemquestions"


def project_questions(apps, schema_editor):
    ItemQuestion = apps.get_model("meli", "ItemQuestion")
    Conversation = apps.get_model("inbox", "Conversation")
    Message = apps.get_model("inbox", "Message")
    for question in ItemQuestion.objects.all():
        status = question.status or ""
        needs_reply = status.upper() == "UNANSWERED" and not question.answer_text
        permalink = question.item_permalink or ""
        if not permalink.startswith(("http://", "https://")):
            permalink = ""
        title = (question.item_title or question.item_id or question.question_id)[:200]
        preview = (question.answer_text or question.text or "")[:200]
        conversation, _created = Conversation.objects.update_or_create(
            organization_id=question.organization_id,
            channel=CHANNEL,
            external_id=(question.question_id or "")[:64],
            defaults={
                "title": title,
                "preview": preview,
                "link_url": permalink[:512],
                "needs_reply": needs_reply,
                "last_message_at": question.answered_at or question.asked_at,
            },
        )
        inbound, _created = Message.objects.update_or_create(
            conversation_id=conversation.pk,
            external_id=f"{question.question_id}:in"[:80],
            defaults={
                "organization_id": question.organization_id,
                "direction": "inbound",
                "body": question.text or "",
                "sent_at": question.asked_at,
            },
        )
        keep = [inbound.pk]
        if question.answer_text:
            outbound, _created = Message.objects.update_or_create(
                conversation_id=conversation.pk,
                external_id=f"{question.question_id}:out"[:80],
                defaults={
                    "organization_id": question.organization_id,
                    "direction": "outbound",
                    "body": question.answer_text,
                    "sent_at": question.answered_at,
                },
            )
            keep.append(outbound.pk)
        (
            Message.objects.filter(conversation_id=conversation.pk)
            .exclude(pk__in=keep)
            .delete()
        )


def unproject_questions(apps, schema_editor):
    Conversation = apps.get_model("inbox", "Conversation")
    Conversation.objects.filter(channel=CHANNEL).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("inbox", "0001_initial"),
        ("meli", "0005_itemquestion"),
    ]

    operations = [
        migrations.RunPython(project_questions, unproject_questions),
    ]
