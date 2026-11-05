"""Mercado Libre listings in Vimple: one row per item or variation, linked to the catalog by SKU.

Nothing here writes to Mercado Libre.
"""
import logging
from datetime import timezone as dt_timezone
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .client import LISTING_STATUSES, describe_failure, fetch_items, fetch_seller_item_ids
from .models import MeliListing, MeliListingSync

logger = logging.getLogger(__name__)

BATCH = 20
UPDATE_FIELDS = [
    "product",
    "seller_sku",
    "title",
    "variation_label",
    "status",
    "sub_status",
    "price",
    "currency",
    "available_quantity",
    "sold_quantity",
    "listing_type_id",
    "category_id",
    "condition",
    "catalog_listing",
    "permalink",
    "thumbnail",
    "logistic_type",
    "inventory_id",
    "meli_created_at",
    "meli_updated_at",
    "synced_at",
    "payload",
]


def sku_of(item, variation=None):
    """The seller's SKU: SELLER_SKU attribute, else seller_custom_field. A variation's own first."""
    sources = [variation] if variation is not None else [item]
    for source in sources:
        if not isinstance(source, dict):
            continue
        for attribute in source.get("attributes") or []:
            if isinstance(attribute, dict) and attribute.get("id") == "SELLER_SKU":
                value = _text(attribute.get("value_name"), 120)
                if value:
                    return value
        value = _text(source.get("seller_custom_field"), 120)
        if value:
            return value
    return ""


def rows_for(item):
    """The rows one Mercado Libre item becomes: one per variation, or one for the item."""
    variations = [variation for variation in item.get("variations") or [] if isinstance(variation, dict)]
    base = {key: value for key, value in item.items() if key != "variations"}
    common = {
        "item_id": _text(item.get("id"), 32),
        "title": _text(item.get("title"), 255),
        "status": _text(item.get("status"), 32),
        "sub_status": [str(part) for part in item.get("sub_status") or []][:10],
        "currency": _text(item.get("currency_id"), 8),
        "listing_type_id": _text(item.get("listing_type_id"), 32),
        "category_id": _text(item.get("category_id"), 32),
        "condition": _text(item.get("condition"), 32),
        "catalog_listing": bool(item.get("catalog_listing")),
        "permalink": _https(item.get("permalink")),
        "thumbnail": _https(item.get("secure_thumbnail") or item.get("thumbnail")),
        "logistic_type": _text((item.get("shipping") or {}).get("logistic_type"), 32),
        "meli_created_at": _when(item.get("date_created")),
        "meli_updated_at": _when(item.get("last_updated")),
    }
    if not variations:
        return [
            {
                **common,
                "variation_id": "",
                "variation_label": "",
                "seller_sku": sku_of(item),
                "price": _money(item.get("price")),
                "available_quantity": _count(item.get("available_quantity")),
                "sold_quantity": _count(item.get("sold_quantity")),
                "inventory_id": _text(item.get("inventory_id"), 32),
                "payload": base,
            }
        ]
    return [
        {
            **common,
            "variation_id": _text(variation.get("id"), 32),
            "variation_label": _label(variation),
            "seller_sku": sku_of(item, variation),
            "price": _money(variation.get("price", item.get("price"))),
            "available_quantity": _count(variation.get("available_quantity")),
            "sold_quantity": _count(variation.get("sold_quantity")),
            "inventory_id": _text(variation.get("inventory_id") or item.get("inventory_id"), 32),
            "payload": {**base, "variation": variation},
        }
        for variation in variations
    ]


def store_items(organization, items):
    """Upsert the rows of these items for one organization and link them by SKU.

    items maps item_id to the item, or to None for an item Mercado Libre no longer has.
    Variations an item no longer has, and items that are gone, are removed.
    """
    from catalog.models import MasterProduct

    now = timezone.now()
    rows = []
    for item in items.values():
        if item:
            rows.extend(row for row in rows_for(item) if row["item_id"])
    skus = {row["seller_sku"].strip().upper() for row in rows if row["seller_sku"]}
    products = dict(
        MasterProduct.objects.filter(organization=organization, sku__in=skus).values_list("sku", "pk")
    )
    listings = [
        MeliListing(
            organization=organization,
            product_id=products.get(row["seller_sku"].strip().upper()),
            synced_at=now,
            **row,
        )
        for row in rows
    ]
    with transaction.atomic():
        if listings:
            MeliListing.objects.bulk_create(
                listings,
                update_conflicts=True,
                unique_fields=["organization", "item_id", "variation_id"],
                update_fields=UPDATE_FIELDS,
            )
        kept = Q()
        for row in rows:
            kept |= Q(item_id=row["item_id"], variation_id=row["variation_id"])
        stale = MeliListing.objects.filter(organization=organization, item_id__in=list(items))
        if rows:
            stale = stale.exclude(kept)
        stale.delete()
    from inventory.ledger import apply_pending_sales

    apply_pending_sales(organization)
    return len(listings)


def sync_listings(organization):
    """Read every listing of this organization's seller, in every status but deleted.

    Rows for items that no longer show up are removed only after every search finished.
    """
    from .postsale import _account_for

    record, _ = MeliListingSync.objects.get_or_create(organization=organization)
    account = _account_for(organization)
    if account is None:
        _finish(record, MeliListingSync.State.FAILED, "Mercado Libre is not connected.")
        return record
    record.state = MeliListingSync.State.RUNNING
    record.started_at = timezone.now()
    record.error = ""
    record.save(update_fields=["state", "started_at", "error", "updated_at"])
    try:
        token = account.fresh_access_token()
        seen = []
        for status in LISTING_STATUSES:
            seen.extend(fetch_seller_item_ids(token, account.meli_user_id, status))
        seen = list(dict.fromkeys(seen))
        for start in range(0, len(seen), BATCH):
            store_items(organization, fetch_items(token, seen[start : start + BATCH]))
        MeliListing.objects.filter(organization=organization).exclude(item_id__in=seen).delete()
        refresh_full_stock(organization, token)
    except Exception as error:
        logger.warning("Listing sync failed for %s: %s", organization.pk, describe_failure(error))
        _finish(record, MeliListingSync.State.FAILED, _reason(error))
        return record
    _finish(record, MeliListingSync.State.OK, "")
    return record


def refresh_listing(organization, item_id):
    """Re-read one item, for an items webhook. A deleted item's rows go away."""
    from .postsale import _account_for

    account = _account_for(organization)
    if account is None or not item_id:
        return 0
    return store_items(organization, fetch_items(account.fresh_access_token(), [item_id]))


def refresh_full_stock(organization, token=None):
    """Mercado Libre's Full stock for every Full listing. One inventory that fails is logged and skipped."""
    from .client import fetch_full_stock
    from .postsale import _account_for

    rows = MeliListing.objects.filter(organization=organization, logistic_type="fulfillment").exclude(inventory_id="")
    inventory_ids = sorted(set(rows.values_list("inventory_id", flat=True)))
    if not inventory_ids:
        return 0
    if token is None:
        account = _account_for(organization)
        if account is None:
            return 0
        token = account.fresh_access_token()
    read = 0
    for inventory_id in inventory_ids:
        try:
            payload = fetch_full_stock(token, inventory_id)
        except (OSError, ValueError) as error:
            logger.warning("Full stock %s could not be read for %s: %s", inventory_id, organization.pk, describe_failure(error))
            continue
        units = _count(payload.get("available_quantity"))
        rows.filter(inventory_id=inventory_id).update(full_available=units)
        read += 1
    return read


def link_product(product):
    """A new catalog product takes the unlinked listings that already carry its SKU."""
    linked = (
        MeliListing.objects.filter(organization_id=product.organization_id, product__isnull=True)
        .filter(seller_sku__iexact=product.sku)
        .update(product=product)
    )
    if linked:
        from inventory.ledger import apply_pending_sales

        apply_pending_sales(product.organization)
    return linked


def link_new_product(sender, instance, created, **kwargs):
    """post_save on catalog.MasterProduct, registered in meli/apps.py so the catalog stays independent."""
    if created:
        link_product(instance)


def summary(organization):
    rows = MeliListing.objects.filter(organization=organization)
    return {
        "total": rows.count(),
        "linked": rows.filter(product__isnull=False).count(),
        "no_sku": rows.filter(product__isnull=True, seller_sku="").count(),
        "unmatched": rows.filter(product__isnull=True).exclude(seller_sku="").count(),
    }


def _finish(record, state, error):
    counts = summary(record.organization)
    record.state = state
    record.error = error[:300]
    record.finished_at = timezone.now()
    for field, value in counts.items():
        setattr(record, field, value)
    record.save()


def _reason(error):
    from .client import MercadoLibreError

    if isinstance(error, MercadoLibreError):
        return f"Mercado Libre said: {error.message}"
    if error.__class__.__name__ == "InvalidToken":
        return "The saved Mercado Libre connection can't be read on this server."
    if isinstance(error, OSError):
        return "Mercado Libre could not be reached. Try again."
    return "The sync stopped on an unexpected error."


def _label(variation):
    parts = []
    for combination in variation.get("attribute_combinations") or []:
        if isinstance(combination, dict) and combination.get("value_name"):
            name = combination.get("name") or ""
            parts.append(f"{name}: {combination['value_name']}" if name else str(combination["value_name"]))
    return ", ".join(parts)[:255]


def _text(value, limit):
    if value is None:
        return ""
    return " ".join(str(value).split())[:limit]


def _https(value):
    value = str(value or "")
    if value.startswith("http://"):
        value = "https://" + value[len("http://") :]
    return value[:512] if value.startswith("https://") else ""


def _money(value):
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return amount if amount.is_finite() and abs(amount) < Decimal("1000000000000") else None


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _when(value):
    if not isinstance(value, str):
        return None
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, dt_timezone.utc)
    return parsed
