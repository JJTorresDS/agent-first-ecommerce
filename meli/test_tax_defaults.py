"""IVA 21 % and Impuesto interno 0 % by default; ids from another attribute's list never go out."""

from .drafts import build_item, defaults_for, invalid_values
from .models import ListingDraft
from .test_product_drafts import PERFUME_ATTRIBUTES, PerfumeDraftCase

IVA = {"id": "VALUE_ADDED_TAX", "name": "IVA", "tags": {}, "value_type": "list", "values": [
    {"id": "55043032", "name": "Exento"}, {"id": "48405909", "name": "21 %"}, {"id": "48405910", "name": "27 %"}]}
DUTY = {"id": "IMPORT_DUTY", "name": "Impuesto interno", "tags": {}, "value_type": "list", "values": [
    {"id": "49553239", "name": "0 %"}, {"id": "49553240", "name": "1 %"}]}


class TaxDefaultTests(PerfumeDraftCase):
    def setUp(self):
        super().setUp()
        self.meli.category_attributes = [*PERFUME_ATTRIBUTES, IVA, DUTY]
        self.create()
        self.draft_row = self.draft()

    def sent(self, draft=None):
        body = build_item(draft or self.draft(), defaults_for(self.organization))
        return {attribute["id"]: attribute for attribute in body["attributes"]}

    def test_every_listing_sends_iva_21_and_no_import_duty(self):
        sent = self.sent()
        self.assertEqual(sent["VALUE_ADDED_TAX"], {"id": "VALUE_ADDED_TAX", "value_id": "48405909"})
        self.assertEqual(sent["IMPORT_DUTY"], {"id": "IMPORT_DUTY", "value_id": "49553239"})
        page = self.client.get(f"/catalog/meli/new/{self.draft_row.pk}/")
        self.assertContains(page, '<option value="21 %" selected>', html=False)

    def test_a_category_without_them_gets_neither(self):
        draft = self.draft()
        draft.category_schema = [item for item in draft.category_schema if item["id"] not in ("VALUE_ADDED_TAX", "IMPORT_DUTY")]
        self.assertNotIn("IMPORT_DUTY", self.sent(draft))
        self.assertNotIn("VALUE_ADDED_TAX", self.sent(draft))

    def test_the_product_and_the_draft_win_and_leave_out_sends_nothing(self):
        self.product.attributes = {**self.product.attributes, "VALUE_ADDED_TAX": "Exento"}
        self.product.save()
        draft = self.draft()
        draft.attributes = {"IMPORT_DUTY": "1 %"}
        sent = self.sent(draft)
        self.assertEqual(sent["VALUE_ADDED_TAX"]["value_id"], "55043032")
        self.assertEqual(sent["IMPORT_DUTY"]["value_id"], "49553240")
        draft.attributes = {"IMPORT_DUTY": None}
        self.assertNotIn("IMPORT_DUTY", self.sent(draft))

    def test_an_ai_fix_with_another_attributes_id_is_not_sent(self):
        draft = self.draft()
        # IVA's 27 % given as Impuesto interno, as a fix once did.
        draft.overrides = {"attributes": {"IMPORT_DUTY": {"value_id": "48405910"}, "VALUE_ADDED_TAX": {"value_id": "48405910"}}}
        sent = self.sent(draft)
        self.assertEqual(sent["IMPORT_DUTY"]["value_id"], "49553239")
        self.assertEqual(sent["VALUE_ADDED_TAX"]["value_id"], "48405910")

    def test_an_id_off_the_list_stops_the_check_before_mercado_libre(self):
        draft = self.draft()
        body = build_item(draft, defaults_for(self.organization))
        body["attributes"].append({"id": "IMPORT_DUTY", "value_id": "48405910"})
        body["attributes"] = list({a["id"]: a for a in body["attributes"]}.values())
        self.assertEqual([(item["id"], item["value"]) for item in invalid_values(draft, body)], [("IMPORT_DUTY", "id 48405910")])
        self.assertEqual(self.draft_row.kind, ListingDraft.Kind.PRODUCT)
