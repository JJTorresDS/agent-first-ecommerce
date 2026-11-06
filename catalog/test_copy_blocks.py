from django.test import TestCase

from accounts.models import Membership, Organization

from .models import Category, CopyBlock
from .test_views import member


class CopyBlockPageTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Seller")
        self.client.force_login(member(self.organization))
        self.perfumes = Category.objects.create(organization=self.organization, code="PERF", name="Perfumes")

    def save(self, url="/catalog/copy/new/", **fields):
        data = {"title": "Advertencias", "kind": "warnings", "category": self.perfumes.pk,
                "body": "<b>Sin garantía</b>", "default_on": "on", "position": "0", **fields}
        return self.client.post(url, data)

    def test_an_owner_writes_edits_and_deletes_copy(self):
        self.assertRedirects(self.save(), "/catalog/copy/")
        block = CopyBlock.objects.get()
        self.assertEqual((block.body, block.category, block.default_on), ("Sin garantía", self.perfumes, True))
        page = self.client.get("/catalog/copy/")
        self.assertContains(page, "Advertencias")
        self.assertContains(page, "New listings")
        self.save(f"/catalog/copy/{block.pk}/", body="Sin cambio una vez abierto", category="")
        block.refresh_from_db()
        self.assertEqual((block.body, block.category), ("Sin cambio una vez abierto", None))
        self.assertContains(self.save(), "You already have copy with this title.")
        self.client.post(f"/catalog/copy/{block.pk}/", {"action": "delete"})
        self.assertFalse(CopyBlock.objects.exists())

    def test_a_category_holding_copy_is_not_deleted(self):
        self.save()
        self.client.post("/catalog/codes/", {"kind": "category", "id": self.perfumes.pk, "action": "delete"})
        self.assertTrue(Category.objects.filter(pk=self.perfumes.pk).exists())

    def test_contributors_read_and_other_organizations_see_nothing(self):
        self.save()
        block = CopyBlock.objects.get()
        self.client.force_login(member(self.organization, role=Membership.Role.CONTRIBUTOR, username="cid"))
        self.assertEqual(self.save().status_code, 403)
        self.assertContains(self.client.get(f"/catalog/copy/{block.pk}/"), "disabled")
        other = Organization.objects.create(name="Other")
        Category.objects.create(organization=other, code="PERF", name="Their perfumes")
        self.client.force_login(member(other, username="bea"))
        self.assertEqual(self.client.get(f"/catalog/copy/{block.pk}/").status_code, 404)
        self.assertNotContains(self.client.get("/catalog/copy/"), "Advertencias")
        self.assertContains(self.client.get("/catalog/copy/new/"), "Their perfumes")
        self.assertEqual(self.save(category=self.perfumes.pk).status_code, 200)
        self.assertEqual(CopyBlock.objects.count(), 1)
