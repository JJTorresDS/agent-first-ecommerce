from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings
from ninja.testing import TestClient

from tasks.api import api
from tasks.models import CashflowMovement, Task

TOKEN = "test-token"


@override_settings(TASKS_API_TOKEN=TOKEN)
class TaskModelTests(TestCase):
    def test_extracts_docs_file_id(self):
        task = Task.objects.create(
            email_address="a@example.com",
            task_title="Write spec",
            task_deepdive_link="https://docs.google.com/document/d/abc123_XYZ/edit",
        )
        self.assertEqual(task.ticket_id, "abc123_XYZ")

    def test_extracts_open_query_id(self):
        task = Task.objects.create(
            email_address="a@example.com",
            task_title="File",
            task_deepdive_link="https://drive.google.com/open?id=fileId99",
        )
        self.assertEqual(task.ticket_id, "fileId99")

    def test_keeps_explicit_ticket_id(self):
        task = Task.objects.create(
            email_address="a@example.com",
            task_title="Write spec",
            task_deepdive_link="https://docs.google.com/document/d/abc123/edit",
            ticket_id="given",
        )
        self.assertEqual(task.ticket_id, "given")

    def test_ignores_non_google_link(self):
        task = Task.objects.create(
            email_address="a@example.com",
            task_title="Elsewhere",
            task_deepdive_link="https://example.com/d/not-a-drive-id",
        )
        self.assertEqual(task.ticket_id, "")


@override_settings(TASKS_API_TOKEN=TOKEN)
class TaskApiTests(TestCase):
    def setUp(self):
        self.client = TestClient(api)
        self.auth = {"Authorization": f"Bearer {TOKEN}"}

    def test_rejects_missing_or_wrong_token(self):
        missing = self.client.get("/tasks")
        self.assertEqual(missing.status_code, 401)
        wrong = self.client.get("/tasks", headers={"Authorization": "Bearer nope"})
        self.assertEqual(wrong.status_code, 401)

    def test_create_list_and_patch(self):
        created = self.client.post(
            "/tasks",
            json={
                "email_address": "agent@example.com",
                "task_title": "Restock",
                "priority": "3-High: Do this week",
                "task_deepdive_link": "https://drive.google.com/file/d/driveFile1/view",
            },
            headers=self.auth,
        )
        self.assertEqual(created.status_code, 201)
        body = created.json()
        self.assertEqual(body["ticket_id"], "driveFile1")
        self.assertEqual(body["task_title"], "Restock")
        self.assertEqual(body["email_address"], "agent@example.com")

        listed = self.client.get("/tasks", headers=self.auth)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.json()), 1)

        by_priority = self.client.get(
            "/tasks",
            query_params={"priority": "3-High: Do this week"},
            headers=self.auth,
        )
        self.assertEqual(len(by_priority.json()), 1)

        done = self.client.get("/tasks?status=Done", headers=self.auth)
        self.assertEqual(done.json(), [])

        patched = self.client.patch(
            f"/tasks/{body['id']}",
            json={"status": "Done", "assignee": "jonas"},
            headers=self.auth,
        )
        self.assertEqual(patched.status_code, 200)
        self.assertEqual(patched.json()["status"], "Done")
        self.assertEqual(patched.json()["assignee"], "jonas")
        self.assertEqual(patched.json()["task_title"], "Restock")

        assigned = self.client.get(
            "/tasks",
            query_params={"assignee": "jonas", "status": "Done"},
            headers=self.auth,
        )
        self.assertEqual(len(assigned.json()), 1)

    def test_rejects_invalid_email(self):
        response = self.client.post(
            "/tasks",
            json={"email_address": "not-an-email", "task_title": "Bad"},
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 422)


class EffortTests(SimpleTestCase):
    def test_rejects_unknown_effort(self):
        task = Task(
            email_address="a@example.com",
            task_title="Restock",
            effort="2h",
        )
        with self.assertRaises(ValidationError):
            task.clean_fields()

    def test_accepts_a_listed_effort(self):
        task = Task(
            email_address="a@example.com",
            task_title="Restock",
            effort=Task.Effort.SHORT,
        )
        task.clean_fields()
        self.assertEqual(task.effort, "Short: half a day")


class PriorityStatusTests(SimpleTestCase):
    def test_rejects_unknown_priority_and_status(self):
        task = Task(
            email_address="a@example.com",
            task_title="Restock",
            priority="urgent",
            status="Later",
        )
        with self.assertRaises(ValidationError):
            task.clean_fields()

    def test_accepts_listed_priority_and_status(self):
        task = Task(
            email_address="a@example.com",
            task_title="Restock",
            priority=Task.Priority.HIGH,
            status=Task.Status.DONE,
        )
        task.clean_fields()
        self.assertEqual(task.priority, "3-High: Do this week")
        self.assertEqual(task.status, "Done")


class CashflowMovementTests(SimpleTestCase):
    def test_sample_row(self):
        movement = CashflowMovement(
            email_address="jonasjere@gmail.com",
            date=date(2026, 10, 7),
            category="compra",
            amount=Decimal("-58000"),
            currency="ARS",
            exchange_rate_at_purchase=Decimal("1560.97"),
            counterparty_name="Jonas Torres",
            description="Compra SD",
            source_file_id="1Vxe7xZSXwXVZG54KikowHsHpt_tjBBja4nCKPORqH2Q",
            white=False,
            doc_moved_to_date_folder=True,
        )
        movement.clean_fields()
        self.assertEqual(movement.date, date(2026, 10, 7))
        self.assertEqual(movement.amount, Decimal("-58000.00"))
        self.assertEqual(movement.exchange_rate_at_purchase, Decimal("1560.9700"))
        self.assertFalse(movement.white)
        self.assertTrue(movement.doc_moved_to_date_folder)

    def test_category_is_required(self):
        movement = CashflowMovement(
            email_address="jonasjere@gmail.com",
            date=date(2026, 10, 7),
            category="",
            amount=Decimal("-58000"),
            currency="ARS",
        )
        with self.assertRaises(ValidationError):
            movement.clean_fields()


class HealthTests(SimpleTestCase):
    def test_health_skips_the_database(self):
        response = self.client.get("/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok")
