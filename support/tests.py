import json
from io import StringIO

from django.core.management import call_command

from accounts.models import Membership
from catalog.test_views import member
from catalog.tests import product
from meli.models import ListingDraft
from meli.test_drafts import DraftTestCase
from meli.tests import shared_seller

from .models import ErrorReport


class ErrorReportTests(DraftTestCase):
    def setUp(self):
        super().setUp()
        self.meli.refuse = "User has not mode me1"
        self.add()
        self.draft_row = self.draft()

    def report(self, note="", pk=None):
        return self.client.post(
            f"/support/report/draft/{pk or self.draft_row.pk}/",
            {"note": note},
            HTTP_REFERER=f"https://vimple.example/catalog/meli/new/{self.draft_row.pk}/",
        )

    def test_a_report_keeps_the_answer_what_was_sent_and_the_settings(self):
        response = self.report("Posting the 885 perfume")
        row = ErrorReport.objects.get()
        self.assertContains(response, f"Reported as #{row.pk}. Thanks.")
        self.assertEqual((row.organization, row.source, row.object_id), (self.organization, "listing_draft", str(self.draft_row.pk)))
        self.assertEqual(row.reported_by.username, "ada")
        self.assertEqual((row.call, row.http_status, row.error_code), ("POST /items/validate", 400, "validation_error"))
        self.assertEqual(row.cause_codes, ["item.title.length", "item.shipping.mode.invalid"])
        self.assertEqual(row.title, "User has not mode me1")
        self.assertEqual(row.details["sent"]["catalog_product_id"], "MLA45601658")
        self.assertEqual(row.note, "Posting the 885 perfume")
        self.assertEqual(row.context["draft"]["settings"]["warranty"], "seller")
        self.assertEqual(row.context["product_sku"], self.product.sku)
        self.assertTrue(row.context["page"].endswith(f"/catalog/meli/new/{self.draft_row.pk}/"))
        self.assertIn("app_version", row.context)
        # Never a token.
        self.assertNotIn("access-1", json.dumps(row.as_dict(), default=str))

    def test_a_repeat_adds_to_the_open_report(self):
        self.report("first")
        response = self.report("second")
        row = ErrorReport.objects.get()
        self.assertEqual(row.occurrences, 2)
        self.assertEqual(row.note, "first\n---\nsecond")
        self.assertContains(response, f"Already reported as #{row.pk}; added to it (2 times).")
        row.status = ErrorReport.Status.RESOLVED
        row.save()
        self.assertIsNotNone(row.resolved_at)
        self.report()
        self.assertEqual(ErrorReport.objects.count(), 2)

    def test_the_details_block_offers_the_report_button(self):
        for url in ("/catalog/meli/new/", f"/catalog/meli/new/{self.draft_row.pk}/"):
            page = self.client.get(url)
            self.assertContains(page, f'hx-post="/support/report/draft/{self.draft_row.pk}/"')
            self.assertContains(page, "Report error")

    def test_only_this_organizations_drafts_with_details_and_only_by_post(self):
        other = shared_seller("Other", user_id="777")
        theirs = ListingDraft.objects.create(
            organization=other, product=product(other, variant="75ML"), price=1, error_details={"call": "x"}
        )
        self.assertEqual(self.report(pk=theirs.pk).status_code, 404)
        clean = ListingDraft.objects.create(organization=self.organization, product=self.product, price=1)
        self.assertEqual(self.report(pk=clean.pk).status_code, 400)
        self.assertEqual(self.client.get(f"/support/report/draft/{self.draft_row.pk}/").status_code, 405)
        self.assertFalse(ErrorReport.objects.exists())

    def test_contributors_report_but_users_without_the_catalog_cant(self):
        self.client.force_login(member(self.organization, role=Membership.Role.CONTRIBUTOR, username="bea"))
        self.assertEqual(self.report().status_code, 200)
        self.client.force_login(member(self.organization, apps=(), username="cid"))
        self.assertEqual(self.report().status_code, 403)
        self.assertEqual(ErrorReport.objects.count(), 1)

    def test_the_command_prints_reports_as_json(self):
        self.report("first")
        out = StringIO()
        call_command("error_reports", "--open", stdout=out)
        reports = json.loads(out.getvalue())
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["details"]["call"], "POST /items/validate")
        ErrorReport.objects.update(status=ErrorReport.Status.RESOLVED)
        out = StringIO()
        call_command("error_reports", "--open", stdout=out)
        self.assertEqual(json.loads(out.getvalue()), [])
        out = StringIO()
        call_command("error_reports", "--id", str(ErrorReport.objects.get().pk), stdout=out)
        self.assertEqual(len(json.loads(out.getvalue())), 1)
