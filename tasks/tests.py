from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings
from ninja.testing import TestClient

from accounts.models import AppAccess, Organization, assign_default_organization
from tasks.api import api
from tasks.models import CashflowMovement, Task, drive_file_url

User = get_user_model()

TOKEN = "test-token"


@override_settings(TASKS_API_TOKEN=TOKEN)
class TaskModelTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Vimple")

    def test_extracts_docs_file_id(self):
        task = Task.objects.create(
            organization=self.organization,
            email_address="a@example.com",
            task_title="Write spec",
            task_deepdive_link="https://docs.google.com/document/d/abc123_XYZ/edit",
        )
        self.assertEqual(task.ticket_id, "abc123_XYZ")

    def test_extracts_open_query_id(self):
        task = Task.objects.create(
            organization=self.organization,
            email_address="a@example.com",
            task_title="File",
            task_deepdive_link="https://drive.google.com/open?id=fileId99",
        )
        self.assertEqual(task.ticket_id, "fileId99")

    def test_keeps_explicit_ticket_id(self):
        task = Task.objects.create(
            organization=self.organization,
            email_address="a@example.com",
            task_title="Write spec",
            task_deepdive_link="https://docs.google.com/document/d/abc123/edit",
            ticket_id="given",
        )
        self.assertEqual(task.ticket_id, "given")

    def test_ignores_non_google_link(self):
        task = Task.objects.create(
            organization=self.organization,
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
        self.organization = Organization.objects.create(name="Vimple")
        self.org_id = str(self.organization.id)

    def test_rejects_missing_or_wrong_token(self):
        missing = self.client.get("/tasks")
        self.assertEqual(missing.status_code, 401)
        wrong = self.client.get("/tasks", headers={"Authorization": "Bearer nope"})
        self.assertEqual(wrong.status_code, 401)

    def test_create_list_and_patch(self):
        created = self.client.post(
            "/tasks",
            json={
                "organization": self.org_id,
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
        self.assertEqual(body["organization"], self.org_id)

        listed = self.client.get(
            "/tasks",
            query_params={"organization": self.org_id},
            headers=self.auth,
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.json()), 1)

        by_priority = self.client.get(
            "/tasks",
            query_params={"organization": self.org_id, "priority": "3-High: Do this week"},
            headers=self.auth,
        )
        self.assertEqual(len(by_priority.json()), 1)

        done = self.client.get(
            "/tasks",
            query_params={"organization": self.org_id, "status": "Done"},
            headers=self.auth,
        )
        self.assertEqual(done.json(), [])

        patched = self.client.patch(
            f"/tasks/{body['id']}",
            json={"organization": self.org_id, "status": "Done", "assignee": "jonas"},
            headers=self.auth,
        )
        self.assertEqual(patched.status_code, 200)
        self.assertEqual(patched.json()["status"], "Done")
        self.assertEqual(patched.json()["assignee"], "jonas")
        self.assertEqual(patched.json()["task_title"], "Restock")

        assigned = self.client.get(
            "/tasks",
            query_params={
                "organization": self.org_id,
                "assignee": "jonas",
                "status": "Done",
            },
            headers=self.auth,
        )
        self.assertEqual(len(assigned.json()), 1)

    def test_rejects_invalid_email(self):
        response = self.client.post(
            "/tasks",
            json={
                "organization": self.org_id,
                "email_address": "not-an-email",
                "task_title": "Bad",
            },
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 422)

    def test_create_without_an_organization_is_rejected(self):
        response = self.client.post(
            "/tasks",
            json={"email_address": "agent@example.com", "task_title": "Restock"},
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(Task.objects.count(), 0)


class EffortTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Vimple")

    def test_rejects_unknown_effort(self):
        task = Task(
            organization=self.organization,
            email_address="a@example.com",
            task_title="Restock",
            effort="2h",
        )
        with self.assertRaises(ValidationError):
            task.clean_fields()

    def test_accepts_a_listed_effort(self):
        task = Task(
            organization=self.organization,
            email_address="a@example.com",
            task_title="Restock",
            effort=Task.Effort.SHORT,
        )
        task.clean_fields()
        self.assertEqual(task.effort, "Short: half a day")


class PriorityStatusTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Vimple")

    def test_rejects_unknown_priority_and_status(self):
        task = Task(
            organization=self.organization,
            email_address="a@example.com",
            task_title="Restock",
            priority="urgent",
            status="Later",
        )
        with self.assertRaises(ValidationError):
            task.clean_fields()

    def test_accepts_listed_priority_and_status(self):
        task = Task(
            organization=self.organization,
            email_address="a@example.com",
            task_title="Restock",
            priority=Task.Priority.HIGH,
            status=Task.Status.DONE,
        )
        task.clean_fields()
        self.assertEqual(task.priority, "3-High: Do this week")
        self.assertEqual(task.status, "Done")


class CashflowMovementTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Vimple")

    def test_sample_row(self):
        movement = CashflowMovement(
            organization=self.organization,
            uploader_email="jonasjere@gmail.com",
            transaction_date=date(2026, 10, 7),
            category="compra",
            amount=Decimal("-58000"),
            currency="ARS",
            exchange_rate_at_purchase=Decimal("1560.97"),
            counterparty_name="Jonas Torres",
            description="Compra SD",
            source_file_id="1Vxe7xZSXwXVZG54KikowHsHpt_tjBBja4nCKPORqH2Q",
            white=False,
        )
        movement.clean_fields()
        self.assertEqual(movement.transaction_date, date(2026, 10, 7))
        self.assertEqual(movement.amount, Decimal("-58000.00"))
        self.assertEqual(movement.exchange_rate_at_purchase, Decimal("1560.9700"))
        self.assertFalse(movement.white)

    def test_organization_is_required(self):
        movement = CashflowMovement(
            uploader_email="jonasjere@gmail.com",
            transaction_date=date(2026, 10, 7),
            category="compra",
            amount=Decimal("-58000"),
            currency="ARS",
        )
        with self.assertRaises(ValidationError):
            movement.full_clean()

    def test_same_source_file_is_allowed_in_another_organization(self):
        other = Organization.objects.create(name="Other")
        shared = {
            "uploader_email": "ada@example.com",
            "transaction_date": date(2026, 10, 7),
            "category": "compra",
            "amount": Decimal("-10"),
            "currency": "ARS",
            "source_file_id": "same-file",
        }
        CashflowMovement.objects.create(organization=self.organization, **shared)
        CashflowMovement.objects.create(organization=other, **shared)
        self.assertEqual(CashflowMovement.objects.filter(source_file_id="same-file").count(), 2)

    def test_category_is_required(self):
        movement = CashflowMovement(
            organization=self.organization,
            uploader_email="jonasjere@gmail.com",
            transaction_date=date(2026, 10, 7),
            category="",
            amount=Decimal("-58000"),
            currency="ARS",
        )
        with self.assertRaises(ValidationError):
            movement.clean_fields()


SOURCE_FILE_ID = "1Vxe7xZSXwXVZG54KikowHsHpt_tjBBja4nCKPORqH2Q"


def cashflow_payload(**overrides):
    payload = {
        "uploader_email": "jonasjere@gmail.com",
        "transaction_date": "2026-10-07",
        "category": "compra",
        "amount": "-58000.00",
        "currency": "ARS",
    }
    payload.update(overrides)
    return payload


@override_settings(TASKS_API_TOKEN=TOKEN)
class CashflowApiTests(TestCase):
    def setUp(self):
        self.client = TestClient(api)
        self.auth = {"Authorization": f"Bearer {TOKEN}"}
        self.organization = Organization.objects.create(name="Vimple")
        self.org_id = str(self.organization.id)
        self.url = f"/cashflow-movements/by-source/{SOURCE_FILE_ID}"

    def test_put_creates_then_updates_same_movement(self):
        created = self.client.put(
            self.url,
            json=cashflow_payload(organization=self.org_id),
            headers=self.auth,
        )
        self.assertEqual(created.status_code, 201)
        body = created.json()
        self.assertTrue(body["id"])
        self.assertEqual(body["source_file_id"], SOURCE_FILE_ID)
        self.assertEqual(body["amount"], "-58000.00")
        self.assertIsNone(body["sub_category"])
        self.assertEqual(body["organization"], self.org_id)

        updated = self.client.put(
            self.url,
            json=cashflow_payload(
                organization=self.org_id,
                amount="-100.00",
                sub_category="insumos",
            ),
            headers=self.auth,
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["id"], body["id"])
        self.assertEqual(updated.json()["amount"], "-100.00")
        self.assertEqual(updated.json()["sub_category"], "insumos")
        self.assertEqual(CashflowMovement.objects.count(), 1)

    def test_create_without_an_organization_is_rejected(self):
        response = self.client.post(
            "/cashflow-movements",
            json=cashflow_payload(),
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(CashflowMovement.objects.count(), 0)


def movement(**overrides):
    data = {
        "uploader_email": "ada@example.com",
        "transaction_date": date(2026, 10, 7),
        "category": "compra",
        "amount": Decimal("-58000.50"),
        "currency": "ARS",
        "counterparty_name": "Jonas Torres",
        "description": "Compra SD",
        "white": False,
    }
    data.update(overrides)
    return CashflowMovement.objects.create(**data)


class CashflowPageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("ada", email="ada@example.com")
        self.organization = assign_default_organization(self.user)
        AppAccess.objects.create(
            user=self.user,
            app=AppAccess.App.CASHFLOW,
            organization=self.organization,
        )
        movement(organization=self.organization)
        movement(
            organization=self.organization,
            transaction_date=date(2026, 10, 8),
            category="venta",
            amount=Decimal("10.00"),
            currency="USD",
            description="Sale",
        )
        self.client.force_login(self.user)

    def test_granted_user_sees_rows_and_filters(self):
        page = self.client.get("/cashflow/")
        self.assertContains(page, "Compra SD")
        self.assertContains(page, "Sale")
        self.assertContains(page, "-58000.50")
        self.assertContains(page, "htmx.org")
        self.assertContains(page, 'nav-label">Finances')
        self.assertContains(page, 'nav-label">Administration')
        self.assertContains(page, 'nav-label">Account')
        self.assertContains(page, 'class="nav-link active" href="/cashflow/"')

        by_category = self.client.get("/cashflow/", {"category": "compra"})
        self.assertContains(by_category, "Compra SD")
        self.assertNotContains(by_category, "Sale")

        by_currency = self.client.get("/cashflow/", {"currency": "USD"})
        self.assertContains(by_currency, "Sale")
        self.assertNotContains(by_currency, "Compra SD")

        by_date = self.client.get("/cashflow/", {"transaction_date": "2026-10-07"})
        self.assertContains(by_date, "Compra SD")
        self.assertNotContains(by_date, "Sale")

        partial = self.client.get("/cashflow/", HTTP_HX_REQUEST="true")
        self.assertContains(partial, "Compra SD")
        self.assertContains(partial, "Export CSV")
        self.assertNotContains(partial, "<h1>")
        self.assertNotContains(partial, "Log out")
        self.assertNotContains(partial, "Finances")

    def test_export_csv_matches_the_filtered_table(self):
        row = CashflowMovement.objects.get(description="Compra SD")
        row.source_file_id = "abc_123"
        row.save()
        document = "https://drive.google.com/file/d/abc_123/view"
        page = self.client.get("/cashflow/")
        self.assertContains(page, document)

        exported = self.client.get("/cashflow/export.csv")
        self.assertEqual(exported["Content-Type"], "text/csv")
        self.assertIn("attachment", exported["Content-Disposition"])
        body = exported.content.decode()
        self.assertIn("Compra SD", body)
        self.assertIn("Sale", body)
        self.assertIn("Subcategory", body)
        self.assertIn(document, body)

        compra = self.client.get("/cashflow/export.csv", {"category": "compra"})
        text = compra.content.decode()
        self.assertIn("Compra SD", text)
        self.assertNotIn("Sale", text)

        other_user = User.objects.create_user("cara", email="cara@example.com")
        other = assign_default_organization(other_user)
        movement(organization=other, description="Hidden books", category="compra")
        hidden = self.client.get("/cashflow/export.csv")
        self.assertNotIn("Hidden books", hidden.content.decode())

    def test_user_without_a_grant_does_not_see_amounts(self):
        outsider = User.objects.create_user("bea", email="bea@example.com")
        assign_default_organization(outsider)
        self.client.force_login(outsider)
        response = self.client.get("/cashflow/")
        self.assertContains(response, "Waiting for access", status_code=403)
        self.assertNotContains(response, "-58000.50", status_code=403)

    def test_superuser_can_open_cashflow_without_a_grant(self):
        admin = User.objects.create_superuser("admin", "admin@example.com", "secret")
        assign_default_organization(admin)
        self.client.force_login(admin)
        response = self.client.get("/cashflow/")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Compra SD")

    def test_user_does_not_see_another_organizations_rows(self):
        other_user = User.objects.create_user("bea", email="bea@example.com")
        other = assign_default_organization(other_user)
        AppAccess.objects.create(
            user=other_user,
            app=AppAccess.App.CASHFLOW,
            organization=other,
        )
        movement(
            organization=other,
            description="Other books",
            category="venta",
            currency="USD",
        )
        self.client.force_login(self.user)
        own = self.client.get("/cashflow/")
        self.assertContains(own, "Compra SD")
        self.assertNotContains(own, "Other books")
        self.client.force_login(other_user)
        theirs = self.client.get("/cashflow/")
        self.assertContains(theirs, "Other books")
        self.assertNotContains(theirs, "Compra SD")


class DriveFileUrlTests(SimpleTestCase):
    def test_builds_a_view_link_for_a_file_id(self):
        self.assertEqual(
            drive_file_url("abc_123"),
            "https://drive.google.com/file/d/abc_123/view",
        )

    def test_blank_or_unsafe_id_has_no_link(self):
        self.assertEqual(drive_file_url(""), "")
        self.assertEqual(drive_file_url("bad id"), "")


class AdminLabelTests(SimpleTestCase):
    def test_admin_shows_finances_and_keeps_the_tasks_app(self):
        from django.apps import apps
        from django.contrib import admin

        config = apps.get_app_config("tasks")
        self.assertEqual(config.label, "tasks")
        self.assertEqual(config.verbose_name, "Finances")
        self.assertEqual(admin.site.index_title, "Finances")


class HealthTests(SimpleTestCase):
    def test_health_skips_the_database(self):
        response = self.client.get("/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok")
