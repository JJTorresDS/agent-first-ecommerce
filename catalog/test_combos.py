from django.core.exceptions import ValidationError
from django.test import TestCase

from accounts.models import Membership, Organization

from .models import Combo, ComboItem
from .test_views import member
from .tests import IMAGE, product


def combo(organization, code="885X2", title="Kit Perfume 885 x2", **fields):
    row = Combo(organization=organization, code=code, title=title, image_url=fields.pop("image_url", IMAGE), **fields)
    row.full_clean()
    row.save()
    return row


class ComboModelTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Seller")
        self.first = product(self.organization, variant="30ML")
        self.second = product(self.organization, variant="100ML")

    def test_the_sku_comes_from_the_code_and_never_changes(self):
        row = combo(self.organization, code="885x2")
        self.assertEqual(row.sku, "COMBO-885X2")
        row.code = "OTHER"
        with self.assertRaises(ValidationError):
            row.save()

    def test_the_title_says_kit_pack_or_combo(self):
        with self.assertRaises(ValidationError):
            combo(self.organization, title="Perfume 885 x2")
        for title in ("Pack 3 perfumes", "COMBO verano", "kit regalo"):
            combo(self.organization, code=title.split()[0].upper()[:4] + "1", title=title)

    def test_can_make_follows_the_scarcest_component(self):
        row = combo(self.organization)
        ComboItem.objects.create(combo=row, product=self.first, quantity=2)
        ComboItem.objects.create(combo=row, product=self.second, quantity=1)
        self.assertEqual(row.can_make({self.first.pk: 5, self.second.pk: 9}), 2)
        self.assertEqual(row.can_make({self.first.pk: 5}), 0)
        self.assertEqual(Combo.objects.get(pk=row.pk).items.first().organization, self.organization)

    def test_a_component_from_another_organization_is_refused(self):
        other = Organization.objects.create(name="Other")
        theirs = product(other, variant="30ML")
        with self.assertRaises(ValidationError):
            ComboItem.objects.create(combo=combo(self.organization), product=theirs, quantity=1)


class ComboPageTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Seller")
        self.client.force_login(member(self.organization))
        self.first = product(self.organization, variant="30ML")
        self.second = product(self.organization, variant="100ML")

    def post(self, url="/catalog/combos/new/", **fields):
        data = {
            "code": "885MIX", "title": "Kit 885 30 ml + 100 ml", "image_url": IMAGE, "status": "active",
            "currency": "ARS", "list_price": "45000",
            "combo_sku": [self.first.sku, self.second.sku], "combo_quantity": ["1", "1"],
        }
        data.update(fields)
        return self.client.post(url, data)

    def test_without_the_package_it_saves_but_the_page_warns_and_highlights(self):
        self.post()
        row = Combo.objects.get()
        page = self.client.get(f"/catalog/combos/{row.pk}/")
        self.assertContains(page, "so this can't be listed yet")
        self.assertContains(page, "fines, shipping overcharges and buyer claims")
        self.assertContains(page, '<div class="field needs-attention">', count=4, html=False)
        self.post(url=f"/catalog/combos/{row.pk}/", weight_g="400", length_cm="8", width_cm="12", height_cm="18")
        page = self.client.get(f"/catalog/combos/{row.pk}/")
        self.assertNotContains(page, '<div class="field needs-attention">', html=False)
        self.assertNotContains(page, "can't be listed yet")

    def test_a_combo_is_saved_with_its_lines(self):
        response = self.post()
        row = Combo.objects.get(organization=self.organization)
        self.assertRedirects(response, f"/catalog/combos/{row.pk}/?saved=1")
        self.assertEqual(
            sorted((item.product.sku, item.quantity) for item in row.items.all()),
            sorted([(self.first.sku, 1), (self.second.sku, 1)]),
        )
        self.post(url=f"/catalog/combos/{row.pk}/", combo_sku=[self.first.sku, self.first.sku], combo_quantity=["1", "2"])
        self.assertEqual([(item.product, item.quantity) for item in row.items.all()], [(self.first, 3)])
        self.assertContains(self.client.get("/catalog/combos/"), "COMBO-885MIX")

    def test_problems_are_shown_and_nothing_is_saved(self):
        self.assertContains(self.post(list_price=""), "This field is required.")
        self.assertContains(self.post(list_price="0"), "greater than or equal to 0.01")
        self.assertContains(self.post(combo_sku=[self.first.sku], combo_quantity=["1"]), "at least 2 units")
        self.assertContains(self.post(combo_sku=["NOPE-1", self.first.sku]), "NOPE-1: no product in the catalog has this SKU.")
        self.assertContains(self.post(combo_quantity=["0", "1"]), "the quantity must be 1 to 100")
        self.assertContains(self.post(title="Perfumes 885"), "Kit, Pack or Combo")
        self.assertFalse(Combo.objects.exists())

    def test_another_organizations_product_cant_be_a_component(self):
        other = Organization.objects.create(name="Other")
        theirs = product(other, variant="50ML", code="900")
        response = self.post(combo_sku=[self.first.sku, theirs.sku])
        self.assertContains(response, f"{theirs.sku}: no product in the catalog has this SKU.")
        self.assertFalse(Combo.objects.exists())

    def test_contributors_read_but_cannot_change_combos(self):
        self.post()
        row = Combo.objects.get()
        self.client.force_login(member(self.organization, role=Membership.Role.CONTRIBUTOR, username="bea"))
        self.assertEqual(self.client.get(f"/catalog/combos/{row.pk}/").status_code, 200)
        self.assertEqual(self.post(url=f"/catalog/combos/{row.pk}/").status_code, 403)
        self.assertEqual(self.post(code="NEW").status_code, 403)
        self.client.force_login(member(self.organization, apps=(), username="cid"))
        self.assertEqual(self.client.get("/catalog/combos/").status_code, 403)
