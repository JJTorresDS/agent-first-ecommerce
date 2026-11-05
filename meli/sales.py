"""Mercado Libre orders as sale rows. The inventory ledger takes them out of stock."""
import logging
import re

from django.utils import timezone

from .client import describe_failure, fetch_order, fetch_orders_since
from .listings import _text, _when
from .models import MeliListing, MeliListingSync, MeliSale, MeliSalesSync

logger = logging.getLogger(__name__)
_ORDER_RESOURCE = re.compile(r"/orders/(\d+)")


def record_order(organization, order):
    """Store each line of one order and apply it to stock. Returns the lines stored."""
    from inventory.ledger import apply_sale

    order_id = _text(order.get("id"), 32)
    if not order_id:
        return 0
    sold_at = _when(order.get("date_closed")) or _when(order.get("date_created")) or timezone.now()
    status = _text(order.get("status"), 32)
    stored = 0
    for line in order.get("order_items") or []:
        if not isinstance(line, dict):
            continue
        item = line.get("item") if isinstance(line.get("item"), dict) else {}
        item_id = _text(item.get("id"), 32)
        quantity = line.get("quantity")
        if not item_id or not isinstance(quantity, int) or quantity < 1:
            continue
        variation_id = _text(item.get("variation_id"), 32)
        listing = _listing(organization, item_id, variation_id)
        sale, _ = MeliSale.objects.update_or_create(
            organization=organization,
            order_id=order_id,
            item_id=item_id,
            variation_id=variation_id,
            defaults={
                "listing": listing,
                "title": _text(item.get("title"), 255),
                "seller_sku": _text(item.get("seller_sku"), 120),
                "quantity": quantity,
                "status": status,
                # The listing's logistic type says whether Mercado Libre shipped it from Full.
                "from_full": bool(listing and listing.is_full()),
                "sold_at": sold_at,
                "payload": {"order_id": order_id, "status": status, "line": line},
            },
        )
        apply_sale(sale)
        stored += 1
    return stored


def _listing(organization, item_id, variation_id):
    rows = MeliListing.objects.filter(organization=organization, item_id=item_id).select_related("product")
    if variation_id:
        exact = rows.filter(variation_id=variation_id).first()
        if exact is not None:
            return exact
    return rows.filter(variation_id="").first() or rows.first()


def load_order_from_notification(job):
    """An orders_v2 webhook: read the order once per organization connected to this seller."""
    from cryptography.fernet import InvalidToken

    from inventory.models import InventorySettings

    from .postsale import _account_for
    from .views import _clip, _organizations_for

    match = _ORDER_RESOURCE.fullmatch(str(job.get("resource") or ""))
    if not match:
        return 0
    stored = 0
    for organization in _organizations_for(_clip(job.get("user_id"), 64)):
        # Before the first stock count sales don't move stock, so don't spend a Mercado Libre call.
        if not InventorySettings.objects.filter(organization=organization, started_at__isnull=False).exists():
            continue
        account = _account_for(organization)
        if account is None:
            continue
        try:
            stored += record_order(organization, fetch_order(account.fresh_access_token(), match.group(1)))
        except (OSError, ValueError, InvalidToken) as error:
            logger.warning(
                "Order %s could not be loaded for %s: %s", match.group(1), organization.pk, describe_failure(error)
            )
    return stored


def sync_sales(organization):
    """Every order since the first stock count, for sales a webhook missed."""
    from inventory.ledger import settings_for

    from .listings import _reason
    from .postsale import _account_for

    record, _ = MeliSalesSync.objects.get_or_create(organization=organization)
    started = settings_for(organization).started_at
    account = _account_for(organization)
    if account is None or started is None:
        record.state = MeliListingSync.State.FAILED
        record.error = "Mercado Libre is not connected." if account is None else "Count the stock first."
        record.finished_at = timezone.now()
        record.save()
        return record
    record.state = MeliListingSync.State.RUNNING
    record.error = ""
    record.save(update_fields=["state", "error", "updated_at"])
    try:
        orders = fetch_orders_since(account.fresh_access_token(), account.meli_user_id, started)
        for order in orders:
            record_order(organization, order)
    except Exception as error:
        logger.warning("Sales sync failed for %s: %s", organization.pk, describe_failure(error))
        record.state = MeliListingSync.State.FAILED
        record.error = _reason(error)[:300]
    else:
        record.state = MeliListingSync.State.OK
        record.orders = len(orders)
    record.finished_at = timezone.now()
    record.save()
    return record
