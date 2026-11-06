from django.http import QueryDict

from catalog.models import Category, CopyBlock

from .description import EMPTY, compose, default_parts, parts_from, warnings
from .models import ListingDraft
from .test_product_drafts import PerfumeDraftCase
from .tests import shared_seller

FORM = {
    "price": "15999", "quantity": "1", "category_id": "MLA1271", "use_defaults": "on",
    "condition": "new", "warranty": "seller", "warranty_days": "30",
    "attr_key": ["PERFUME_TYPE"], "attr_value": ["Eau de parfum"],
}


class DescriptionTests(PerfumeDraftCase):
    def setUp(self):
        super().setUp()
        category = self.product.product_model.category
        other = Category.objects.create(organization=self.organization, code="ROPA", name="Ropa")
        self.about = CopyBlock.objects.create(
            organization=self.organization, title="Quiénes somos", kind="about", body="Somos expertos.", default_on=True
        )
        self.warn = CopyBlock.objects.create(
            organization=self.organization, title="Advertencias", kind="warnings", category=category,
            body="Sin garantía.", default_on=True,
        )
        self.why = CopyBlock.objects.create(
            organization=self.organization, title="Por qué", kind="why_us", body="Envío en el día.", default_on=False
        )
        CopyBlock.objects.create(
            organization=self.organization, title="Talles", kind="other", category=other, body="Ver tabla.", default_on=True
        )

    def test_a_new_draft_starts_with_the_product_then_the_default_copy_in_order(self):
        self.assertEqual(
            default_parts(self.organization, self.product),
            [{"product": True}, {"block": self.about.pk}, {"block": self.warn.pk}],
        )
        self.create()
        draft = self.draft()
        self.assertEqual(draft.description_parts, default_parts(self.organization, self.product))
        self.assertEqual(compose(draft), "Eau de parfum amaderada de 30 ml.\n\nSomos expertos.\n\nSin garantía.")
        page = self.client.get(f"/catalog/meli/new/{draft.pk}/")
        self.assertContains(page, 'data-part="block:%d"' % self.why.pk)
        self.assertNotContains(page, "Ver tabla.")
        self.assertContains(page, "Somos expertos.\n\nSin garantía.")

    def test_the_seller_orders_the_pieces_and_the_posted_text_is_kept(self):
        self.create()
        draft = self.draft()
        response = self.client.post(f"/catalog/meli/new/{draft.pk}/", {
            **FORM, "desc_present": "1",
            "desc_part": [f"block:{self.why.pk}", "text", "product"], "desc_text": ["Perfume árabe 100 ml."],
        })
        self.assertRedirects(response, "/catalog/meli/new/")
        draft.refresh_from_db()
        self.assertEqual(draft.description_parts, [{"block": self.why.pk}, {"text": "Perfume árabe 100 ml."}, {"product": True}])
        # A block edited before posting is posted as it reads then.
        self.why.body = "Envío gratis."
        self.why.save()
        self.client.post("/catalog/meli/new/", {"action": "post_all"})
        draft.refresh_from_db()
        sent = "Envío gratis.\n\nPerfume árabe 100 ml.\n\nEau de parfum amaderada de 30 ml."
        self.assertEqual(self.meli.descriptions, [{"plain_text": sent}])
        self.assertEqual(draft.posted_description, sent)
        self.why.body = "Otra cosa."
        self.why.save()
        draft.refresh_from_db()
        self.assertEqual(draft.posted_description, sent)

    def test_another_organizations_block_and_a_deleted_one_never_count(self):
        foreign = CopyBlock.objects.create(organization=shared_seller("Other"), title="X", body="Ajeno.")
        post = QueryDict(mutable=True)
        post.setlist("desc_part", [f"block:{foreign.pk}", f"block:{self.about.pk}"])
        self.assertEqual(parts_from(post, self.organization), [{"block": self.about.pk}])
        draft = ListingDraft(organization=self.organization, product=self.product,
                             description_parts=[{"block": foreign.pk}, {"block": self.about.pk}])
        self.assertEqual(compose(draft), "Somos expertos.")
        self.about.delete()
        self.assertEqual(compose(draft), "")

    def test_an_empty_description_stops_the_post(self):
        self.product.product_model.description = ""
        self.product.product_model.save()
        self.create()
        draft = self.draft()
        self.client.post(f"/catalog/meli/new/{draft.pk}/", {**FORM, "desc_present": "1", "desc_part": ["product"]})
        self.client.post("/catalog/meli/new/", {"action": "post_all"})
        draft.refresh_from_db()
        self.assertEqual((draft.status, draft.error), (ListingDraft.Status.FAILED, EMPTY))
        self.assertEqual(self.meli.created, [])

    def test_contact_details_are_flagged(self):
        self.assertEqual(warnings("ok"), [])
        found = " ".join(warnings("Visitá www.tienda.com o escribí a ventas@tienda.com, tel 11 4567-8901"))
        self.assertIn("link", found)
        self.assertIn("email", found)
        self.assertIn("phone", found)
        self.assertIn(EMPTY, warnings("  "))
