from django.contrib.auth import get_user_model
from django.test import TestCase

from accounts.models import Membership, Organization
from meli.models import ItemQuestion

User = get_user_model()


class AlertPanelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("ada", email="ada@example.com")
        self.organization = Organization.objects.create(name="SCENT")
        Membership.objects.create(
            user=self.user,
            organization=self.organization,
            role=Membership.Role.OWNER,
        )
        self.client.force_login(self.user)

    def test_three_unanswered_questions_are_one_group(self):
        for number in range(3):
            ItemQuestion.objects.create(
                organization=self.organization,
                question_id=str(number),
                text="Tienen stock?",
                status="UNANSWERED",
            )
        page = self.client.get("/organization/")
        self.assertEqual(page.context["alert_count"], 3)
        self.assertContains(page, 'class="badge">3<')
        self.assertContains(
            page,
            'href="/inbox/">You have 3 unanswered presales questions<',
        )

    def test_one_unanswered_question_uses_the_singular(self):
        ItemQuestion.objects.create(
            organization=self.organization,
            question_id="1",
            status="UNANSWERED",
        )
        page = self.client.get("/organization/")
        self.assertContains(page, "You have 1 unanswered presales question")
        self.assertNotContains(page, "presales questions")

    def test_an_answered_question_adds_no_group(self):
        ItemQuestion.objects.create(
            organization=self.organization,
            question_id="1",
            status="ANSWERED",
            answer_text="Si hay",
        )
        page = self.client.get("/organization/")
        self.assertEqual(page.context["alert_count"], 0)
        self.assertContains(page, "No alerts.")
        self.assertNotContains(page, 'class="badge"')
        self.assertNotContains(page, "unanswered presales")

    def test_another_organization_stays_out_of_the_panel(self):
        other = Organization.objects.create(name="Other")
        ItemQuestion.objects.create(
            organization=other,
            question_id="9",
            status="UNANSWERED",
        )
        page = self.client.get("/organization/")
        self.assertEqual(page.context["alert_count"], 0)
        self.assertContains(page, "No alerts.")
        self.assertNotContains(page, "unanswered presales")
        other_user = User.objects.create_user("bea", email="bea@example.com")
        Membership.objects.create(
            user=other_user,
            organization=other,
            role=Membership.Role.OWNER,
        )
        self.client.force_login(other_user)
        theirs = self.client.get("/organization/")
        self.assertEqual(theirs.context["alert_count"], 1)
        self.assertContains(theirs, "You have 1 unanswered presales question")

    def test_user_without_an_organization_sees_no_bell(self):
        ItemQuestion.objects.create(
            organization=self.organization,
            question_id="1",
            status="UNANSWERED",
        )
        outsider = User.objects.create_user("cleo", email="cleo@example.com")
        self.client.force_login(outsider)
        page = self.client.get("/organization/setup/")
        self.assertEqual(page.context["alert_count"], 0)
        self.assertEqual(page.context["alert_groups"], [])
        self.assertNotContains(page, 'class="alerts"')
