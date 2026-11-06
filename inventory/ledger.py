"""The only place stock changes. Every change is a StockMovement; on-hand is their sum."""
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from .models import InventorySettings, StockLocation, StockMovement

Kind = StockMovement.Kind
OWN = StockLocation.Kind.OWN
FULL = StockLocation.Kind.MELI_FULL
# Manual changes and the sign each kind takes.
MANUAL_KINDS = {Kind.RETURN: 1, Kind.DAMAGE: -1, Kind.ADJUSTMENT: 0}


def location(organization, kind):
    row, _ = StockLocation.objects.get_or_create(
        organization=organization,
        kind=kind,
        defaults={"name": StockLocation.Kind(kind).label},
    )
    return row


def settings_for(organization):
    row, _ = InventorySettings.objects.get_or_create(organization=organization)
    return row


def on_hand(product):
    """{own: units, meli_full: units} for one product."""
    totals = {OWN: 0, FULL: 0}
    rows = (
        StockMovement.objects.filter(organization_id=product.organization_id, product=product)
        .values("location__kind")
        .annotate(units=Sum("quantity"))
    )
    for row in rows:
        totals[row["location__kind"]] = row["units"] or 0
    return totals


def on_hand_by_product(organization):
    """{product_id: {own: units, meli_full: units}} for the whole organization, in one query."""
    totals = {}
    rows = (
        StockMovement.objects.filter(organization=organization)
        .values("product_id", "location__kind")
        .annotate(units=Sum("quantity"))
    )
    for row in rows:
        totals.setdefault(row["product_id"], {OWN: 0, FULL: 0})[row["location__kind"]] = row["units"] or 0
    return totals


def record(organization, product, kind, where, quantity, *, source_key="", by="", note="", at=None, **sources):
    """Write one movement. With a source_key, a second call for the same source does nothing."""
    if source_key and StockMovement.objects.filter(organization=organization, source_key=source_key).exists():
        return None
    movement = StockMovement(
        organization=organization,
        product=product,
        location=location(organization, where),
        quantity=quantity,
        kind=kind,
        source_key=source_key,
        created_by=by[:254],
        note=note[:300],
        occurred_at=at or timezone.now(),
        **sources,
    )
    try:
        with transaction.atomic():
            movement.save()
    except IntegrityError:
        if source_key:
            # Another worker recorded the same source a moment ago.
            return None
        raise
    return movement


def count(organization, product, where, counted, *, opening=False, by="", at=None):
    """A stock take: write the difference between what was counted and what Vimple has.

    opening marks the first count, which also starts taking Mercado Libre sales (start()).
    """
    if counted < 0:
        raise ValidationError("A count can't be negative.")
    difference = counted - on_hand(product)[where]
    if not difference:
        return None
    kind = Kind.OPENING if opening else Kind.COUNT
    return record(organization, product, kind, where, difference, by=by, at=at, note=f"Counted {counted}")


def start(organization, *, by="", at=None):
    """Mark the first count. Returns True only the first time."""
    current = settings_for(organization)
    if current.started_at:
        return False
    current.started_at = at or timezone.now()
    current.started_by = by[:254]
    current.save(update_fields=["started_at", "started_by"])
    return True


def adjust(organization, product, kind, units, *, by="", note=""):
    """A return (+), damage or loss (-), or a signed adjustment, in the Depósito."""
    if kind not in MANUAL_KINDS:
        raise ValidationError("Choose return, damage or adjustment.")
    if not units:
        raise ValidationError("Enter how many units.")
    sign = MANUAL_KINDS[kind]
    quantity = abs(units) * sign if sign else units
    return record(organization, product, kind, OWN, quantity, by=by, note=note)


def send_to_full(organization, product, units, *, by="", note=""):
    """Depósito to Mercado Libre Full: two movements, one out and one in."""
    if units <= 0:
        raise ValidationError("Send at least one unit.")
    with transaction.atomic():
        record(organization, product, Kind.TRANSFER_OUT, OWN, -units, by=by, note=note)
        record(organization, product, Kind.TRANSFER_IN, FULL, units, by=by, note=note)


def received(item):
    """Units of a purchase line received so far."""
    return item.receipts.aggregate(units=Sum("quantity"))["units"] or 0


def receive(purchase, quantities, *, by="", at=None):
    """Receive purchase lines into the Depósito. quantities maps line id to units.

    Refuses more than a line still expects. All lines or none.
    """
    from purchasing.models import PurchaseItem

    errors = []
    with transaction.atomic():
        lines = {
            item.pk: item
            for item in PurchaseItem.objects.select_for_update()
            .filter(organization_id=purchase.organization_id, purchase=purchase)
            .select_related("product")
        }
        plan = []
        for line_id, units in quantities.items():
            item = lines.get(line_id)
            if item is None or not units:
                continue
            remaining = item.quantity - received(item)
            if units < 0 or units > remaining:
                errors.append(f"{item.product.sku}: {remaining} still to receive.")
                continue
            plan.append((item, units))
        if errors:
            raise ValidationError(errors)
        if not plan:
            raise ValidationError("Enter how many units arrived.")
        for item, units in plan:
            record(
                purchase.organization,
                item.product,
                Kind.RECEIPT,
                OWN,
                units,
                by=by,
                at=at,
                note=purchase.invoice_number or "",
                purchase_item=item,
            )


def apply_sale(sale):
    """Take a Mercado Libre sale out of stock, or put it back when it's cancelled.

    Counts only sales after the first stock count, on a listing linked to a product or a combo.
    A combo's sale takes each component (its quantity in the combo times the units sold).
    Each order line is taken once and put back once, whatever arrives how many times.
    """
    listing = sale.listing
    started = settings_for(sale.organization).started_at
    if listing is None or started is None or sale.sold_at < started:
        return False
    units = sale.quantity * max(1, listing.units_per_sale)
    if listing.product_id is not None:
        # One movement, keyed as before, so sales already taken keep their key.
        parts = [(listing.product, units, "")]
    elif listing.combo_id is not None:
        parts = [
            (item.product, units * item.quantity, f":{item.product_id}")
            for item in listing.combo.items.select_related("product")
        ]
    else:
        return False
    if not parts:
        return False
    where = FULL if sale.from_full else OWN
    line = f"{sale.order_id}:{sale.item_id}:{sale.variation_id}"
    taken = StockMovement.objects.filter(
        organization=sale.organization, source_key=f"sale:{line}{parts[0][2]}"
    ).exists()
    if sale.counts():
        # record skips a key it already has, so a repeat (or a combo cut short) only adds what's missing.
        for product, quantity, suffix in parts:
            record(
                sale.organization, product, Kind.SALE, where, -quantity,
                source_key=f"sale:{line}{suffix}", by="Mercado Libre", at=sale.sold_at,
                note=f"Order {sale.order_id}", meli_sale=sale,
            )
        taken = True
    if sale.cancelled() and taken:
        for product, quantity, suffix in parts:
            record(
                sale.organization, product, Kind.SALE_CANCEL, where, quantity,
                source_key=f"cancel:{line}{suffix}", by="Mercado Libre",
                note=f"Order {sale.order_id} cancelled", meli_sale=sale,
            )
    if taken and not sale.applied:
        sale.applied = True
        sale.save(update_fields=["applied", "updated_at"])
    return taken


def apply_pending_sales(organization):
    """Sales that waited for a listing link or a first count."""
    from meli.models import MeliSale

    applied = 0
    pending = MeliSale.objects.filter(organization=organization, applied=False).filter(
        Q(listing__product__isnull=False) | Q(listing__combo__isnull=False)
    )
    for sale in pending.select_related("listing", "organization"):
        applied += bool(apply_sale(sale))
    return applied
