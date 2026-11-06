import re
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

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
        self.assertEqual(movement.receipt_id, "")
        self.assertEqual(movement.document_type, "")
        self.assertIsNone(movement.amount_parsing_confidence)
        self.assertIsNone(movement.parsing_observations)
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
        self.assertEqual(body["receipt_id"], "")
        self.assertEqual(body["document_type"], "")
        self.assertIsNone(body["amount_parsing_confidence"])
        self.assertIsNone(body["parsing_observations"])
        self.assertEqual(body["amount"], "-58000.00")
        self.assertIsNone(body["sub_category"])
        self.assertEqual(body["organization"], self.org_id)

        updated = self.client.put(
            self.url,
            json=cashflow_payload(
                organization=self.org_id,
                amount="-100.00",
                sub_category="insumos",
                receipt_id="FAC-1042",
                document_type="invoice",
            ),
            headers=self.auth,
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["id"], body["id"])
        self.assertEqual(updated.json()["amount"], "-100.00")
        self.assertEqual(updated.json()["sub_category"], "insumos")
        self.assertEqual(updated.json()["receipt_id"], "FAC-1042")
        self.assertEqual(updated.json()["document_type"], "invoice")
        self.assertEqual(CashflowMovement.objects.count(), 1)

    def test_amount_parsing_confidence_is_stored_between_zero_and_one(self):
        created = self.client.put(
            self.url,
            json=cashflow_payload(
                organization=self.org_id,
                amount_parsing_confidence="0.850",
                parsing_observations={"failed_checks": ["total"]},
            ),
            headers=self.auth,
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["amount_parsing_confidence"], "0.850")
        self.assertEqual(created.json()["parsing_observations"], {"failed_checks": ["total"]})
        stored = CashflowMovement.objects.get()
        self.assertEqual(stored.amount_parsing_confidence, Decimal("0.850"))
        self.assertEqual(stored.parsing_observations, {"failed_checks": ["total"]})

        cleared = self.client.patch(
            f"/cashflow-movements/{created.json()['id']}",
            json={"organization": self.org_id, "amount_parsing_confidence": None},
            headers=self.auth,
        )
        self.assertEqual(cleared.status_code, 200)
        self.assertIsNone(cleared.json()["amount_parsing_confidence"])
        self.assertEqual(cleared.json()["parsing_observations"], {"failed_checks": ["total"]})

        rejected = self.client.put(
            self.url,
            json=cashflow_payload(organization=self.org_id, amount_parsing_confidence="1.500"),
            headers=self.auth,
        )
        self.assertEqual(rejected.status_code, 422)
        self.assertIsNone(CashflowMovement.objects.get().amount_parsing_confidence)

    def test_document_type_must_be_receipt_or_invoice(self):
        response = self.client.put(
            self.url,
            json=cashflow_payload(organization=self.org_id, document_type="other"),
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(CashflowMovement.objects.count(), 0)

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
        self.assertNotContains(page, 'nav-label">Account')
        body = page.content.decode()
        self.assertLess(body.index('nav-label">Administration'), body.index('nav-label">Finances'))
        self.assertContains(page, 'class="nav-link active" title="Cashflow" href="/cashflow/"')
        self.assertContains(page, 'aria-label="Collapse menu"')
        self.assertContains(page, "vimple-sidebar")
        self.assertContains(page, 'hx-get="/profile/menu/"')
        self.assertContains(page, "Page 1 of 1")
        self.assertNotContains(page, 'name="page"')

        by_category = self.client.get("/cashflow/", {"category": "compra"})
        self.assertContains(by_category, "Compra SD")
        self.assertNotContains(by_category, "Sale")

        by_currency = self.client.get("/cashflow/", {"currency": "USD"})
        self.assertContains(by_currency, "Sale")
        self.assertNotContains(by_currency, "Compra SD")

        one_day = self.client.get(
            "/cashflow/",
            {"date_from": "2026-10-07", "date_to": "2026-10-07"},
        )
        self.assertContains(one_day, "Compra SD")
        self.assertNotContains(one_day, "Sale")

        both_days = self.client.get(
            "/cashflow/",
            {"date_from": "2026-10-08", "date_to": "2026-10-07"},
        )
        self.assertContains(both_days, "Compra SD")
        self.assertContains(both_days, "Sale")
        self.assertContains(both_days, 'id="date_from"')
        self.assertContains(both_days, 'value="2026-10-07"')
        self.assertContains(both_days, 'value="2026-10-08"')

        exported = self.client.get(
            "/cashflow/export.csv",
            {"date_from": "2026-10-08"},
        )
        text = exported.content.decode()
        self.assertIn("Sale", text)
        self.assertNotIn("Compra SD", text)

        partial = self.client.get("/cashflow/", HTTP_HX_REQUEST="true")
        self.assertContains(partial, "Compra SD")
        self.assertContains(partial, "Export CSV")
        self.assertContains(partial, 'class="pager"')
        self.assertNotContains(partial, "<h1>")
        self.assertNotContains(partial, "Log out")
        self.assertNotContains(partial, "Finances")

    def test_long_description_offers_more(self):
        row = CashflowMovement.objects.get(description="Compra SD")
        row.description = "Compra de utensilios de cocina " * 8
        row.save()
        page = self.client.get("/cashflow/")
        self.assertContains(page, "…more")
        self.assertContains(page, "Compra de utensilios de cocina")
        self.assertContains(page, 'class="description-text clamped"')
        self.assertContains(page, "Page 1 of 1")

        row.parsing_observations = {"note": "Total was hard to read. " * 8}
        row.save()
        observations = self.client.get("/cashflow/")
        self.assertContains(observations, "Total was hard to read.")
        self.assertEqual(observations.content.decode().count('class="more"'), 2)
        self.assertEqual(observations.content.decode().count('class="description-text clamped"'), 2)

    def test_pages_keep_filters_and_csv_exports_every_row(self):
        CashflowMovement.objects.filter(organization=self.organization).delete()
        for index in range(26):
            movement(
                organization=self.organization,
                transaction_date=date(2026, 1, 1) + timedelta(days=index),
                category="venta" if index == 0 else "compra",
                description=f"Movement {index:02d}",
            )

        first = self.client.get("/cashflow/")
        self.assertContains(first, "Movement 25")
        self.assertContains(first, "Page 1 of 2")
        self.assertNotContains(first, "Movement 00")

        second = self.client.get("/cashflow/", {"page": "2"})
        self.assertContains(second, "Movement 00")
        self.assertContains(second, "Previous")
        self.assertNotContains(second, "Movement 25")

        filtered = self.client.get("/cashflow/", {"page": "2", "category": "compra"})
        self.assertContains(filtered, "Movement 25")
        self.assertNotContains(filtered, "Movement 00")

        exported = self.client.get("/cashflow/export.csv")
        text = exported.content.decode()
        self.assertIn("Movement 00", text)
        self.assertIn("Movement 25", text)

    def test_export_csv_matches_the_filtered_table(self):
        row = CashflowMovement.objects.get(description="Compra SD")
        row.source_file_id = "abc_123"
        row.receipt_id = "FAC-1042"
        row.document_type = "invoice"
        row.save()
        document = "https://drive.google.com/file/d/abc_123/view"
        page = self.client.get("/cashflow/")
        self.assertContains(page, document)
        self.assertContains(page, "FAC-1042")
        self.assertContains(page, "Invoice")

        exported = self.client.get("/cashflow/export.csv")
        self.assertEqual(exported["Content-Type"], "text/csv")
        self.assertIn("attachment", exported["Content-Disposition"])
        body = exported.content.decode()
        self.assertIn("Compra SD", body)
        self.assertIn("Sale", body)
        self.assertIn("Subcategory", body)
        self.assertIn("Receipt", body)
        self.assertIn("Document type", body)
        self.assertIn("FAC-1042", body)
        self.assertIn("invoice", body)
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

    def test_filters_by_amount_parsing_confidence(self):
        low = CashflowMovement.objects.get(description="Compra SD")
        low.amount_parsing_confidence = Decimal("0.200")
        low.parsing_observations = {"note": "Total was hard to read."}
        low.save()
        high = CashflowMovement.objects.get(description="Sale")
        high.amount_parsing_confidence = Decimal("0.900")
        high.save()
        movement(organization=self.organization, description="Unscored")

        page = self.client.get("/cashflow/")
        self.assertContains(page, "Amount confidence")
        self.assertContains(page, "Observations")
        self.assertContains(page, 'id="amount_confidence_from"')
        self.assertContains(page, "0.200")
        self.assertContains(page, "0.900")
        self.assertContains(page, "Total was hard to read.")
        self.assertContains(page, "{&quot;note&quot;: &quot;Total was hard to read.&quot;}")
        self.assertContains(page, "Unscored")

        low_only = self.client.get("/cashflow/", {"amount_confidence_to": "0.500"})
        self.assertContains(low_only, "Compra SD")
        self.assertContains(low_only, "Total was hard to read.")
        self.assertNotContains(low_only, "Sale")
        self.assertNotContains(low_only, "Unscored")
        self.assertContains(low_only, 'value="0.500"')
        self.assertContains(low_only, "amount_confidence_to=0.500")

        high_only = self.client.get("/cashflow/", {"amount_confidence_from": "0.800"})
        self.assertContains(high_only, "Sale")
        self.assertNotContains(high_only, "Compra SD")
        self.assertNotContains(high_only, "Unscored")

        swapped = self.client.get(
            "/cashflow/",
            {"amount_confidence_from": "0.900", "amount_confidence_to": "0.200"},
        )
        self.assertContains(swapped, "Compra SD")
        self.assertContains(swapped, "Sale")
        self.assertNotContains(swapped, "Unscored")
        self.assertContains(
            swapped,
            'id="amount_confidence_from" type="number" name="amount_confidence_from" min="0" max="1" step="0.001" value="0.200"',
        )
        self.assertContains(
            swapped,
            'id="amount_confidence_to" type="number" name="amount_confidence_to" min="0" max="1" step="0.001" value="0.900"',
        )

        ignored = self.client.get("/cashflow/", {"amount_confidence_from": "nope"})
        self.assertContains(ignored, "Unscored")
        self.assertContains(
            ignored,
            'id="amount_confidence_from" type="number" name="amount_confidence_from" min="0" max="1" step="0.001" value=""',
        )

        exported = self.client.get("/cashflow/export.csv", {"amount_confidence_from": "0.800"})
        text = exported.content.decode()
        self.assertIn("Amount confidence", text)
        self.assertIn("Observations", text)
        listed = self.client.get("/cashflow/export.csv").content.decode()
        self.assertIn('"{""note"": ""Total was hard to read.""}"', listed)
        self.assertIn("Sale", text)
        self.assertIn("0.900", text)
        self.assertNotIn("Compra SD", text)
        self.assertNotIn("Unscored", text)

        other_user = User.objects.create_user("erin", email="erin@example.com")
        other = assign_default_organization(other_user)
        movement(
            organization=other,
            description="Hidden confidence",
            amount_parsing_confidence=Decimal("0.900"),
            parsing_observations={"note": "Other org note"},
        )
        hidden = self.client.get("/cashflow/", {"amount_confidence_from": "0.800"})
        self.assertContains(hidden, "Sale")
        self.assertNotContains(hidden, "Hidden confidence")
        self.assertNotContains(hidden, "Other org note")

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


FOLDERS = {
    "receipts_folder": "1owUI7-o1n3MHrTJ8WHQjsG8NtnQOs4i1",
    "cost.sueldos": "1FSYa6chwZych7vTx45dWy3jQDlHQwzjX",
    "cost.servicios": "1A8SuBUHRLkCgW4LbdVu1Qezc7uKLwep5",
    "cost.insumos": "1Z3YK0DRe1dtPI1O_wkhATzVGN1DtfDF4",
    "cost.productos": "1uOkXMAOK74vwJCIRHAaH2Iy7K4GnFnBK",
    "profit.productos": "1Qwsej_CQ1vP-Ncuk_-MpWsNPGWAqZ5Be",
    "profit.servicios": "1WoyEqvI5HPKxcUcIsXXtsvKMXyZERW2y",
}


class _ParserResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b'{"ok": true}'


class _GoogleResponse(_ParserResponse):
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        import json

        return json.dumps(self.payload).encode()


@override_settings(
    GOOGLE_PICKER_API_KEY="picker-key",
    GOOGLE_CLOUD_PROJECT_NUMBER="123456",
    GOOGLE_CLIENT_ID="client-id",
    GOOGLE_CLIENT_SECRET="client-secret",
)
class CashflowSettingsTests(TestCase):
    def setUp(self):
        from accounts.models import Membership

        self.organization = Organization.objects.create(name="Moli TLA")
        self.user = User.objects.create_user("ada", email="ada@example.com")
        Membership.objects.create(
            user=self.user, organization=self.organization, role=Membership.Role.OWNER
        )
        AppAccess.objects.create(
            user=self.user, app=AppAccess.App.CASHFLOW, organization=self.organization
        )
        self.other = Organization.objects.create(name="Other org")
        self.client.force_login(self.user)

    def _member(self, username, role, organization=None):
        from accounts.models import Membership

        organization = organization or self.organization
        user = User.objects.create_user(username, email=f"{username}@example.com")
        Membership.objects.create(user=user, organization=organization, role=role)
        AppAccess.objects.create(user=user, app=AppAccess.App.CASHFLOW, organization=organization)
        return user

    def _save_all(self, key="parser-key"):
        response = self.client.post("/cashflow/settings/", FOLDERS)
        self.client.post("/cashflow/settings/key/", {"action": "save", "api_key": key})
        return response

    def _google_token(self, user, token="live-token", expires_in=3600, refresh="refresh-1"):
        from allauth.socialaccount.models import SocialAccount, SocialToken
        from django.utils import timezone

        account = SocialAccount.objects.create(user=user, provider="google", uid=f"uid-{user.pk}")
        return SocialToken.objects.create(
            account=account,
            token=token,
            token_secret=refresh,
            expires_at=timezone.now() + timedelta(seconds=expires_in),
        )

    def test_settings_tab_saves_ids_and_drive_links_for_our_organization_only(self):
        from tasks.models import CashflowParserSettings

        CashflowParserSettings.objects.create(
            organization=self.other,
            folders={"receipts_folder": {"id": "OtherOrgFolder123", "name": "Other secret folder"}},
        )
        listing = self.client.get("/cashflow/")
        self.assertContains(listing, 'href="/cashflow/settings/"')
        values = dict(FOLDERS)
        values["receipts_folder"] = (
            "https://drive.google.com/drive/folders/1owUI7-o1n3MHrTJ8WHQjsG8NtnQOs4i1?usp=sharing"
        )
        values["receipts_folder__name"] = "Recibos"
        response = self.client.post("/cashflow/settings/", values)
        self.assertRedirects(response, "/cashflow/settings/?saved=1")
        mine = CashflowParserSettings.objects.get(organization=self.organization)
        self.assertEqual(mine.folder("receipts_folder"), {"id": FOLDERS["receipts_folder"], "name": "Recibos"})
        self.assertEqual(mine.folder("profit.servicios")["id"], FOLDERS["profit.servicios"])
        theirs = CashflowParserSettings.objects.get(organization=self.other)
        self.assertEqual(theirs.folder("receipts_folder")["id"], "OtherOrgFolder123")
        page = self.client.get("/cashflow/settings/")
        self.assertContains(page, FOLDERS["cost.insumos"])
        self.assertContains(page, "Recibos")
        self.assertNotContains(page, "OtherOrgFolder123")
        self.assertNotContains(page, "Other secret folder")

    def test_bad_folder_values_are_rejected(self):
        from tasks.models import CashflowParserSettings, folder_id

        self.assertEqual(folder_id("https://evil.example.com/folders/abc"), "")
        self.assertEqual(folder_id("not a folder!"), "")
        self.assertEqual(folder_id(" abc_DEF-123 "), "abc_DEF-123")
        response = self.client.post(
            "/cashflow/settings/", {**FOLDERS, "cost.sueldos": "https://evil.example.com/folders/abc"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Not a Drive folder ID or link.", status_code=400)
        self.assertFalse(
            CashflowParserSettings.objects.filter(organization=self.organization).exclude(folders={}).exists()
        )

    def test_contributor_sees_settings_but_cannot_change_or_run(self):
        from accounts.models import Membership

        contributor = self._member("bea", Membership.Role.CONTRIBUTOR)
        self._save_all()
        self.client.force_login(contributor)
        page = self.client.get("/cashflow/settings/")
        self.assertContains(page, FOLDERS["cost.sueldos"])
        self.assertContains(page, "readonly")
        self.assertNotContains(page, "Run receipt parser")
        self.assertNotContains(page, "Connect Google Drive")
        with patch("tasks.parser.urlopen", side_effect=AssertionError("network")):
            self.assertEqual(self.client.post("/cashflow/settings/", FOLDERS).status_code, 403)
            self.assertEqual(self.client.post("/cashflow/settings/run/").status_code, 403)
            self.assertEqual(self.client.post("/cashflow/settings/drive-token/").status_code, 403)

    def test_user_without_cashflow_access_gets_the_pending_page(self):
        from accounts.models import Membership

        outsider = User.objects.create_user("cid", email="cid@example.com")
        Membership.objects.create(user=outsider, organization=self.organization, role=Membership.Role.ADMIN)
        self.client.force_login(outsider)
        self.assertEqual(self.client.get("/cashflow/settings/").status_code, 403)
        self.assertEqual(self.client.post("/cashflow/settings/run/").status_code, 403)

    def test_run_sends_the_n8n_body_with_the_key_for_our_organization(self):
        import json

        from tasks.models import CashflowParserSettings
        from tasks.parser import RECEIPT_PARSER_URL

        CashflowParserSettings.objects.create(
            organization=self.other,
            folders={slot: {"id": "OtherOrgFolder123", "name": ""} for slot in FOLDERS},
        )
        self._save_all()
        sent = []

        def open_parser(request, timeout=30):
            sent.append(request)
            return _ParserResponse()

        with patch("tasks.parser.urlopen", side_effect=open_parser):
            response = self.client.post("/cashflow/settings/run/")
        self.assertRedirects(response, "/cashflow/settings/")
        self.assertEqual(len(sent), 1)
        request = sent[0]
        self.assertEqual(request.full_url, RECEIPT_PARSER_URL)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("X-api-key"), "parser-key")
        org = str(self.organization.pk)
        self.assertEqual(
            json.loads(request.data),
            {
                "organization_id": org,
                "files": [
                    {
                        "organization_id": org,
                        "organization_name": "Moli TLA",
                        "receipts_folder": FOLDERS["receipts_folder"],
                        "cost": {
                            "sueldos": FOLDERS["cost.sueldos"],
                            "servicios": FOLDERS["cost.servicios"],
                            "insumos": FOLDERS["cost.insumos"],
                            "productos": FOLDERS["cost.productos"],
                        },
                        "profit": {
                            "productos": FOLDERS["profit.productos"],
                            "servicios": FOLDERS["profit.servicios"],
                        },
                    }
                ],
            },
        )
        self.assertNotIn("OtherOrgFolder123", request.data.decode())
        mine = CashflowParserSettings.objects.get(organization=self.organization)
        self.assertEqual(mine.last_run_status, "ok")
        self.assertEqual(mine.last_run_by, "ada@example.com")
        self.assertEqual(CashflowParserSettings.objects.get(organization=self.other).last_run_status, "")
        page = self.client.get("/cashflow/settings/")
        self.assertContains(page, "The receipt parser started")
        self.assertNotContains(page, "parser-key")

    def test_failed_run_retries_three_times_and_hides_the_key(self):
        from urllib.error import HTTPError

        from tasks.models import CashflowParserSettings

        self._save_all()
        tries = []

        def refuse(request, timeout=30):
            tries.append(request)
            raise HTTPError(request.full_url, 500, "parser-key broke", {}, None)

        with patch("tasks.parser.urlopen", side_effect=refuse), patch("tasks.parser.time.sleep") as slept:
            self.client.post("/cashflow/settings/run/")
        self.assertEqual(len(tries), 3)
        self.assertEqual(slept.call_count, 2)
        mine = CashflowParserSettings.objects.get(organization=self.organization)
        self.assertEqual(mine.last_run_status, "failed")
        self.assertNotIn("parser-key", mine.last_run_error)
        page = self.client.get("/cashflow/settings/")
        self.assertContains(page, "did not start")
        self.assertNotContains(page, "parser-key")

    def test_run_refuses_missing_folders_missing_key_or_a_run_in_progress(self):
        from tasks.models import CashflowParserSettings

        with patch("tasks.parser.urlopen", side_effect=AssertionError("network")):
            response = self.client.post("/cashflow/settings/run/")
            self.assertRedirects(response, "/cashflow/settings/?run=nokey", fetch_redirect_response=False)
            page = self.client.get(response["Location"])
            self.assertContains(page, "Add the receipt parser key first.")
            self.client.post("/cashflow/settings/key/", {"action": "save", "api_key": "parser-key"})
            response = self.client.post("/cashflow/settings/run/")
            self.assertRedirects(response, "/cashflow/settings/?run=missing", fetch_redirect_response=False)
            page = self.client.get(response["Location"])
            self.assertContains(page, "Choose every folder before running the parser.")
            self._save_all()
            self.client.post("/cashflow/settings/key/", {"action": "remove"})
            response = self.client.post("/cashflow/settings/run/")
            self.assertRedirects(response, "/cashflow/settings/?run=nokey", fetch_redirect_response=False)
            self.client.post("/cashflow/settings/key/", {"action": "save", "api_key": "parser-key"})
            CashflowParserSettings.objects.filter(organization=self.organization).update(last_run_status="running")
            self.client.post("/cashflow/settings/run/")
            status = self.client.get("/cashflow/settings/", headers={"HX-Request": "true"})
        self.assertContains(status, 'hx-trigger="every 3s"')
        self.assertNotContains(status, "<html")

    def test_drive_token_is_only_the_signed_in_admins_own(self):
        from accounts.models import Membership

        admin = self._member("dee", Membership.Role.ADMIN)
        self._google_token(admin, token="dee-token")
        page = self.client.get("/cashflow/settings/")
        self.assertContains(page, "Connect Google Drive")
        self.assertContains(page, "process=connect")
        self.assertContains(page, "drive.file")
        self.assertNotContains(page, "Choose in Drive")
        response = self.client.post("/cashflow/settings/drive-token/")
        self.assertEqual(response.status_code, 409)
        self.assertNotIn(b"dee-token", response.content)
        self._google_token(self.user, token="ada-token")
        response = self.client.post("/cashflow/settings/drive-token/")
        self.assertEqual(response.json(), {"access_token": "ada-token"})
        self.assertEqual(response["Cache-Control"], "no-store")
        page = self.client.get("/cashflow/settings/")
        self.assertContains(page, "Choose in Drive")
        self.assertContains(page, 'data-key="picker-key"')
        self.assertEqual(self.client.get("/cashflow/settings/drive-token/").status_code, 405)

    def test_expired_drive_token_is_refreshed(self):
        stored = self._google_token(self.user, token="old-token", expires_in=-10)
        calls = []

        def google(request, timeout=15):
            calls.append(request)
            return _GoogleResponse({"access_token": "new-token", "expires_in": 3599})

        with patch("tasks.drive.urlopen", side_effect=google):
            response = self.client.post("/cashflow/settings/drive-token/")
        self.assertEqual(response.json(), {"access_token": "new-token"})
        self.assertIn(b"refresh_token=refresh-1", calls[0].data)
        stored.refresh_from_db()
        self.assertEqual(stored.token, "new-token")

    @override_settings(GOOGLE_PICKER_API_KEY="", GOOGLE_CLOUD_PROJECT_NUMBER="")
    def test_without_picker_settings_there_are_no_drive_buttons(self):
        self._google_token(self.user)
        page = self.client.get("/cashflow/settings/")
        self.assertNotContains(page, "Choose in Drive")
        self.assertNotContains(page, "Connect Google Drive")
        self.assertNotContains(page, "apis.google.com")
        self.assertContains(page, "Save folders")


    def test_key_is_saved_encrypted_and_never_shown(self):
        from tasks.models import CashflowParserSettings

        secret = "n8n-secret-Abc123"
        response = self.client.post("/cashflow/settings/key/", {"action": "save", "api_key": f"  {secret}  "})
        self.assertRedirects(response, "/cashflow/settings/?key=saved")
        mine = CashflowParserSettings.objects.get(organization=self.organization)
        self.assertNotIn(secret, mine.api_key)
        self.assertEqual(mine.api_key_value(), secret)
        page = self.client.get("/cashflow/settings/?key=saved")
        html = page.content.decode()
        self.assertNotIn(secret, html)
        self.assertNotIn(mine.api_key, html)
        self.assertIn("••••••••••••", html)
        self.assertIn("Key saved", html)
        key_input = re.search(r'<input type="password"[^>]*>', html).group(0)
        self.assertNotIn("value=", key_input)
        self.assertIn('autocomplete="new-password"', key_input)

    def test_empty_save_keeps_the_key_and_remove_clears_it(self):
        from tasks.models import CashflowParserSettings

        self.client.post("/cashflow/settings/key/", {"action": "save", "api_key": "first-key"})
        response = self.client.post("/cashflow/settings/key/", {"action": "save", "api_key": "   "})
        self.assertRedirects(response, "/cashflow/settings/?key=empty", fetch_redirect_response=False)
        self.assertContains(self.client.get(response["Location"]), "Enter the key.")
        mine = CashflowParserSettings.objects.get(organization=self.organization)
        self.assertEqual(mine.api_key_value(), "first-key")
        self.client.post("/cashflow/settings/key/", {"action": "remove"})
        mine.refresh_from_db()
        self.assertEqual(mine.api_key, "")
        self.assertContains(self.client.get("/cashflow/settings/"), "No key yet.")

    def test_each_organization_sends_only_its_own_key(self):
        from accounts.models import Membership
        from tasks.models import CashflowParserSettings

        other_admin = self._member("eve", Membership.Role.OWNER, organization=self.other)
        self._save_all(key="ours-key")
        self.client.force_login(other_admin)
        self._save_all(key="theirs-key")
        self.assertEqual(
            CashflowParserSettings.objects.get(organization=self.organization).api_key_value(), "ours-key"
        )
        sent = []

        def open_parser(request, timeout=30):
            sent.append(request)
            return _ParserResponse()

        with patch("tasks.parser.urlopen", side_effect=open_parser):
            self.client.post("/cashflow/settings/run/")
            self.client.force_login(self.user)
            self.client.post("/cashflow/settings/run/")
        self.assertEqual([r.get_header("X-api-key") for r in sent], ["theirs-key", "ours-key"])
        self.assertIn(str(self.other.pk), sent[0].data.decode())
        self.assertNotIn(str(self.organization.pk), sent[0].data.decode())
        page = self.client.get("/cashflow/settings/")
        self.assertNotContains(page, "theirs-key")
        self.assertNotContains(page, "ours-key")

    def test_contributor_cannot_see_or_change_the_key(self):
        from accounts.models import Membership

        self._save_all(key="hidden-key")
        contributor = self._member("fay", Membership.Role.CONTRIBUTOR)
        self.client.force_login(contributor)
        page = self.client.get("/cashflow/settings/")
        self.assertContains(page, "Key saved.")
        self.assertNotContains(page, 'name="api_key"')
        self.assertNotContains(page, "hidden-key")
        response = self.client.post("/cashflow/settings/key/", {"action": "remove"})
        self.assertEqual(response.status_code, 403)
        from tasks.models import CashflowParserSettings

        self.assertEqual(
            CashflowParserSettings.objects.get(organization=self.organization).api_key_value(), "hidden-key"
        )

    def test_key_saved_under_another_secret_key_asks_again(self):
        from tasks.models import CashflowParserSettings

        self._save_all()
        mine = CashflowParserSettings.objects.get(organization=self.organization)
        with override_settings(SECRET_KEY="an-old-secret-key"):
            mine.set_api_key("old-key")
        mine.save(update_fields=["api_key"])
        self.assertTrue(mine.api_key)
        self.assertFalse(mine.has_api_key())
        page = self.client.get("/cashflow/settings/")
        self.assertContains(page, "The saved key can")
        with patch("tasks.parser.urlopen", side_effect=AssertionError("network")):
            response = self.client.post("/cashflow/settings/run/")
        self.assertRedirects(response, "/cashflow/settings/?run=nokey", fetch_redirect_response=False)


class CashflowMovementPageTests(TestCase):
    def setUp(self):
        from catalog.test_views import member

        self.member = member
        self.organization = Organization.objects.create(name="Vimple")
        self.client.force_login(member(self.organization, apps=(AppAccess.App.CASHFLOW,)))
        self.row = movement(organization=self.organization)

    def url(self, row=None):
        return f"/cashflow/movements/{(row or self.row).pk}/"

    def test_the_list_links_to_it_and_an_owner_corrects_it(self):
        self.assertContains(self.client.get("/cashflow/"), self.url())
        data = {
            "transaction_date": "2026-10-08", "category": "compra", "amount": "-60000", "currency": "ARS",
            "counterparty_name": "Otro", "description": "Corregido", "document_type": "",
        }
        response = self.client.post(self.url(), data)
        self.assertRedirects(response, self.url() + "?saved=1")
        self.row.refresh_from_db()
        self.assertEqual((self.row.amount, self.row.counterparty_name), (Decimal("-60000"), "Otro"))

    def test_deleting_keeps_its_purchase_unlinked(self):
        from catalog.tests import product
        from purchasing.models import Purchase, Supplier

        supplier = Supplier.objects.create(organization=self.organization, name="Sur")
        purchase = Purchase.objects.create(
            organization=self.organization, supplier=supplier, purchased_on=date(2026, 10, 7), cashflow_movement=self.row
        )
        self.assertContains(self.client.get(self.url()), "the next parser run adds it again")
        response = self.client.post(self.url(), {"action": "delete"})
        self.assertRedirects(response, "/cashflow/", fetch_redirect_response=False)
        self.assertFalse(CashflowMovement.objects.exists())
        purchase.refresh_from_db()
        self.assertIsNone(purchase.cashflow_movement_id)

    def test_contributors_read_only_and_other_organizations_not_at_all(self):
        from accounts.models import Membership

        self.client.force_login(
            self.member(self.organization, role=Membership.Role.CONTRIBUTOR, username="cid", apps=(AppAccess.App.CASHFLOW,))
        )
        page = self.client.get(self.url())
        self.assertContains(page, "disabled")
        self.assertNotContains(page, 'data-dialog="delete-dialog"')
        self.assertEqual(self.client.post(self.url(), {"action": "delete"}).status_code, 403)
        other = Organization.objects.create(name="Other")
        self.client.force_login(self.member(other, username="bea", apps=(AppAccess.App.CASHFLOW,)))
        self.assertEqual(self.client.get(self.url()).status_code, 404)
        self.assertTrue(CashflowMovement.objects.exists())
