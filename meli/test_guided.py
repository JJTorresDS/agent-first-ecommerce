from .drafts import schema_of
from .models import ListingDraft
from .test_product_drafts import PERFUME_ATTRIBUTES, PerfumeDraftCase

VOLUME_WITH_UNITS = {
    "id": "UNIT_VOLUME", "name": "Volumen de la unidad", "tags": {"required": True}, "value_type": "number_unit",
    "allowed_units": [{"id": "mL", "name": "mL"}, {"id": "L", "name": "L"}], "default_unit": "mL",
    "hint": "El volumen del envase.",
}


class GuidedAttributeTests(PerfumeDraftCase):
    """The draft page asks for the category's attributes with Mercado Libre's own choices and units."""

    def setUp(self):
        super().setUp()
        self.meli.category_attributes = [
            VOLUME_WITH_UNITS if item["id"] == "UNIT_VOLUME" else item for item in PERFUME_ATTRIBUTES
        ]

    def test_required_fields_use_the_lists_and_units_and_the_rest_wait_under_recommended(self):
        self.create()
        draft = self.draft()
        self.assertEqual(draft.category_schema[2]["units"], ["mL", "L"])
        page = self.client.get(f"/catalog/meli/new/{draft.pk}/").content.decode()
        required, recommended = page.split("<legend>Required by Mercado Libre</legend>")[1].split("<details")
        # A list attribute is a select of Mercado Libre's values, still empty and highlighted.
        self.assertIn('<select name="attr_value" id="ga-PERFUME_TYPE">', required)
        self.assertIn('<option value="Eau de toilette">', required)
        self.assertIn('<div class="field needs-attention">\n  <input type="hidden" name="attr_key" value="PERFUME_TYPE">', required)
        # The product's "30 mL" splits into the number and the unit.
        self.assertIn('id="ga-UNIT_VOLUME" value="30"', required)
        self.assertIn('<option value="mL" selected>mL</option>', required)
        self.assertIn("El volumen del envase.", required)
        # Brand, GTIN and the reason for no GTIN come from the product, not from these fields.
        self.assertNotIn('value="BRAND"', page)
        self.assertNotIn('id="ga-EMPTY_GTIN_REASON"', page)
        self.assertIn('id="ga-OLFACTORY_NOTES" value="Amaderado; Cítrico"', recommended)

    def test_saving_joins_number_and_unit_and_drops_empty_fields(self):
        self.create()
        draft = self.draft()
        self.client.post(
            f"/catalog/meli/new/{draft.pk}/",
            {
                "price": "15999", "quantity": "1", "category_id": "MLA1271", "use_defaults": "on",
                "condition": "new", "warranty": "seller", "warranty_days": "30",
                "attr_key": ["PERFUME_TYPE", "UNIT_VOLUME", "OLFACTORY_NOTES", "COLOR"],
                "attr_value": ["Eau de parfum", "50", "", "Azul"],
                "unit_UNIT_VOLUME": "mL",
            },
        )
        draft.refresh_from_db()
        self.assertEqual(draft.attributes, {"PERFUME_TYPE": "Eau de parfum", "UNIT_VOLUME": "50 mL", "COLOR": "Azul"})
        self.assertEqual(draft.status, ListingDraft.Status.READY, draft.error)
        page = self.client.get(f"/catalog/meli/new/{draft.pk}/")
        # COLOR isn't one of the category's: it stays a free row.
        self.assertContains(page, '<input type="text" name="attr_key" value="COLOR" aria-label="Attribute">', html=False)

    def test_before_the_first_check_the_page_waits_for_the_attributes(self):
        self.create()
        draft = self.draft()
        ListingDraft.objects.filter(pk=draft.pk).update(category_schema=[], status=ListingDraft.Status.CHECKING)
        self.assertContains(self.client.get(f"/catalog/meli/new/{draft.pk}/"), "Loading Mercado Libre's attributes")

    def test_schema_keeps_units_hints_and_hidden(self):
        hidden = {"id": "X", "name": "X", "tags": {"hidden": True, "required": True}}
        schema = schema_of([VOLUME_WITH_UNITS, hidden])
        self.assertEqual((schema[0]["units"], schema[0]["default_unit"]), (["mL", "L"], "mL"))
        self.assertEqual((schema[1]["hidden"], schema[1]["required"]), (True, False))
