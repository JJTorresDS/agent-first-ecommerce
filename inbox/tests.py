from datetime import timedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.models import Organization

from .models import Conversation, Message
from .services import upsert_thread


def bubbles(count, start=None):
    start = start or timezone.now() - timedelta(hours=1)
    return [
        {
            "external_id": f"m{index}",
            "direction": Message.Direction.INBOUND if index % 2 else Message.Direction.OUTBOUND,
            "body": f"Mensaje {index}",
            "sent_at": start + timedelta(minutes=index),
        }
        for index in range(count)
    ]


def project(organization, messages, external_id="2000015861321589"):
    return upsert_thread(
        organization=organization,
        channel="meli-postsales",
        external_id=external_id,
        title="Test Test",
        preview=messages[-1]["body"] if messages else "",
        link_url="",
        needs_reply=True,
        last_message_at=messages[-1]["sent_at"] if messages else None,
        messages=messages,
    )


class ThreadWriteTests(TestCase):
    """Every query is a round trip to Neon, so writing a chat must not cost one query per message."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Seller")

    def test_a_long_chat_is_written_in_a_handful_of_queries(self):
        messages = bubbles(35)
        project(self.organization, messages)
        messages.append({"external_id": "m35", "direction": "inbound", "body": "Nuevo", "sent_at": timezone.now()})
        with CaptureQueriesContext(connection) as queries:
            conversation = project(self.organization, messages)
        self.assertLessEqual(len(queries), 10, [query["sql"][:80] for query in queries])
        self.assertEqual(conversation.messages.count(), 36)
        with CaptureQueriesContext(connection) as unchanged:
            project(self.organization, messages)
        updates = [query for query in unchanged if query["sql"].startswith("UPDATE") and "inbox_message" in query["sql"]]
        self.assertEqual(updates, [])

    def test_new_changed_and_missing_messages_are_written(self):
        messages = bubbles(4)
        conversation = project(self.organization, messages)
        before = conversation.messages.get(external_id="m1").updated_at
        messages[1] = {**messages[1], "body": "Editado"}
        del messages[3]
        messages.append({"external_id": "m9", "direction": "inbound", "body": "Otro", "sent_at": timezone.now()})
        project(self.organization, messages)
        rows = {row.external_id: row for row in conversation.messages.all()}
        self.assertEqual(sorted(rows), ["m0", "m1", "m2", "m9"])
        self.assertEqual(rows["m1"].body, "Editado")
        self.assertGreater(rows["m1"].updated_at, before)
        self.assertEqual(rows["m9"].organization, self.organization)

    def test_another_organizations_chat_with_the_same_id_is_untouched(self):
        other = Organization.objects.create(name="Other")
        theirs = project(other, bubbles(3))
        mine = project(self.organization, bubbles(2))
        self.assertNotEqual(mine.pk, theirs.pk)
        self.assertEqual(theirs.messages.count(), 3)
        self.assertEqual(set(theirs.messages.values_list("organization_id", flat=True)), {other.id})
        self.assertEqual(set(mine.messages.values_list("organization_id", flat=True)), {self.organization.id})

    def test_a_sent_reply_is_one_row_and_replaces_the_pending_bubble(self):
        from meli.postsale import remember_reply

        conversation = project(self.organization, bubbles(35))
        Message.objects.create(
            organization=self.organization, conversation=conversation, external_id="pending",
            direction="outbound", body="Hola", sent_at=timezone.now(),
        )
        with CaptureQueriesContext(connection) as queries:
            remember_reply(conversation, "Hola", timezone.now(), "msg-real")
        self.assertLessEqual(len(queries), 6, [query["sql"][:80] for query in queries])
        self.assertFalse(conversation.messages.filter(external_id="pending").exists())
        self.assertEqual(conversation.messages.get(external_id="msg-real").body, "Hola")
        self.assertEqual(conversation.messages.count(), 36)
        conversation.refresh_from_db()
        self.assertFalse(conversation.needs_reply)
        self.assertEqual(conversation.preview, "Hola")
        # Stored again by the background re-read: still one row.
        remember_reply(conversation, "Hola", timezone.now(), "msg-real")
        self.assertEqual(conversation.messages.filter(external_id="msg-real").count(), 1)
