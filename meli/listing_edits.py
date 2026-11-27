"""Price and available quantity of a Mercado Libre listing, changed from the Listings page.

The page writes a ListingChange (who, why, old → new) and queues it; the worker sends it here, one PUT /items,
and ends the change done or failed with Mercado Libre's reason. Quantity never goes above what the Depósito
holds (once inventory has started), checked on the page and again here, and never for a Full listing.
"""

import logging
from decimal import Decimal, InvalidOperation

from .client import describe_failure
from .models import ListingChange, MeliListing

logger = logging.getLogger(__name__)

EDIT_FIELDS = (ListingChange.Field.PRICE, ListingChange.Field.QUANTITY)
MAX_QUANTITY = 99999


def _reason(error):
    from .drafts import _reason

    return _reason(error)


def read_price(raw):
    """A price above zero with at most two decimals ("1.234,50" and "1234.5" both read), else None."""
    text = str(raw or "").strip().replace(" ", "")
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    if not value.is_finite() or value <= 0 or value >= Decimal("1e12") or value != value.quantize(Decimal("0.01")):
        return None
    return value.quantize(Decimal("0.01"))


def read_quantity(raw):
    try:
        value = int(str(raw or "").strip())
    except ValueError:
        return None
    return value if 0 <= value <= MAX_QUANTITY else None


def stock_limits(organization, listings):
    """{listing pk: most units it may offer}, or no entry when there's no cap.

    Linked to a product: its Depósito stock; to a combo: how many complete combos it makes; divided by
    units per sale. No cap before the first inventory count, or for a listing linked to nothing.
    """
    from .drafts import inventory_started, stock_by_product

    linked = [row for row in listings if row.product_id or row.combo_id]
    if not linked or not inventory_started(organization):
        return {}
    stock = stock_by_product(organization)
    limits = {}
    for row in linked:
        units = row.combo.can_make(stock) if row.combo_id and not row.product_id else stock.get(row.product_id, 0)
        limits[row.pk] = max(0, units) // max(1, row.units_per_sale)
    return limits


def edit_problem(listing, field, value, limit=None):
    """Why this change can't be sent, or ""."""
    if listing.status == "closed":
        return "A closed listing can't change."
    if field == ListingChange.Field.QUANTITY:
        if listing.is_full():
            return "This listing is in Full: Mercado Libre counts its stock."
        if limit is not None and value > limit:
            return f"Only {limit} in the Depósito for this listing; lower the quantity."
    return ""


def _body(listing, field, value, siblings):
    """PUT /items body. With variations, every one is named; a price goes on all, a quantity on this one."""
    number = float(value) if field == ListingChange.Field.PRICE else int(value)
    if not listing.variation_id:
        return {field: number}
    variations = []
    for row in siblings:
        entry = {"id": int(row.variation_id) if row.variation_id.isdigit() else row.variation_id}
        if field == ListingChange.Field.PRICE or row.variation_id == listing.variation_id:
            entry[field] = number
        variations.append(entry)
    return {"variations": variations}


def apply_change(organization, change_id):
    """The worker's half: send one queued price or quantity change, then read the item back."""
    from .client import update_item
    from .listing_templates import _token
    from .listings import refresh_listing

    change = ListingChange.objects.filter(
        organization=organization, pk=change_id, state=ListingChange.State.QUEUED, field__in=EDIT_FIELDS
    ).first()
    if change is None:
        return
    siblings = list(MeliListing.objects.filter(organization=organization, item_id=change.item_id).select_related("combo"))
    listing = next((row for row in siblings if row.variation_id == change.variation_id), None)
    if listing is None and not change.variation_id and siblings:
        listing = siblings[0]  # A price is the whole item's, every variation.
    if listing is None:
        change.finish("This listing is no longer on Mercado Libre.")
        return
    value = read_price(change.new_value) if change.field == ListingChange.Field.PRICE else read_quantity(change.new_value)
    if value is None:
        change.finish("Not a valid value.")
        return
    limit = stock_limits(organization, [listing]).get(listing.pk) if change.field == ListingChange.Field.QUANTITY else None
    problem = edit_problem(listing, change.field, value, limit)
    if problem:
        listing.action_error = problem[:300]
        listing.save(update_fields=["action_error"])
        change.finish(problem)
        return
    verb = f"set the {change.get_field_display().lower()}"
    try:
        update_item(_token(organization), change.item_id, _body(listing, change.field, value, siblings))
    except Exception as error:
        logger.warning("Couldn't %s of %s: %s", verb, change.item_id, describe_failure(error))
        message = f"Couldn't {verb}: {_reason(error)}"[:300]
        MeliListing.objects.filter(pk=listing.pk).update(action_error=message)
        change.finish(message)
        return
    rows = MeliListing.objects.filter(organization=organization, item_id=change.item_id)
    if change.field == ListingChange.Field.QUANTITY:
        rows = rows.filter(pk=listing.pk)
    rows.update(**{change.field: value, "action_error": ""})
    change.finish()
    if change.field == ListingChange.Field.QUANTITY:
        send_package(organization, change, listing)
    try:
        refresh_listing(organization, change.item_id)
    except Exception as error:
        logger.warning("Listing %s changed but wasn't read back: %s", change.item_id, describe_failure(error))


def _measure(text):
    """ "17 cm", "17.0 cm" and "17 CM" read the same: (17.0, "cm"); None when it isn't a number and a unit."""
    parts = str(text or "").strip().replace(",", ".").split()
    if len(parts) != 2:
        return None
    try:
        return float(parts[0]), parts[1].lower()
    except ValueError:
        return None


def package_update(listing):
    """The SELLER_PACKAGE_ attributes to send so the listing carries its product's package, or [].

    Only from a product or combo with all four measures, and only when one differs from the listing's.
    """
    from catalog.models import package_missing

    from .drafts import package_attributes

    listed = listing.product if listing.product_id else listing.combo
    if listed is None or package_missing(listed):
        return []
    wanted = package_attributes(listed)
    current = {
        attribute.get("id"): _measure(attribute.get("value_name"))
        for attribute in (listing.payload or {}).get("attributes") or []
        if isinstance(attribute, dict)
    }
    if all(current.get(attribute["id"]) == _measure(attribute["value_name"]) for attribute in wanted):
        return []
    return wanted


def _package_text(attributes):
    values = {attribute["id"]: attribute.get("value_name") or "" for attribute in attributes}
    size = " × ".join(
        values.get(key, "?").removesuffix(" cm")
        for key in ("SELLER_PACKAGE_HEIGHT", "SELLER_PACKAGE_WIDTH", "SELLER_PACKAGE_LENGTH")
    )
    return f"{size} cm, {values.get('SELLER_PACKAGE_WEIGHT', '?')}"[:64]


def send_package(organization, change, listing):
    """After a stock change: the product's package to the listing too, as its own logged change.

    A refusal is noted on the row and in the log; the quantity change stays done.
    """
    from .client import update_item
    from .listing_templates import _token

    wanted = package_update(listing)
    if not wanted:
        return
    current = [
        attribute for attribute in (listing.payload or {}).get("attributes") or []
        if isinstance(attribute, dict) and attribute.get("id") in {item["id"] for item in wanted}
    ]
    logged = ListingChange.objects.create(
        organization=organization, item_id=change.item_id, title=change.title, field=ListingChange.Field.PACKAGE,
        old_value=_package_text(current) if current else "", new_value=_package_text(wanted),
        reason=change.reason, note="Sent with the stock change.", user=change.user,
    )
    try:
        update_item(_token(organization), change.item_id, {"attributes": wanted})
    except Exception as error:
        logger.warning("Package of %s not updated: %s", change.item_id, describe_failure(error))
        message = f"Stock set; package not updated: {_reason(error)}"[:300]
        MeliListing.objects.filter(organization=organization, item_id=change.item_id).update(action_error=message)
        logged.finish(message)
        return
    logged.finish()
