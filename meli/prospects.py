"""Prospect listings: Mercado Libre listings someone else sells, scraped and sent to POST /api/meli/prospects.

The scraper's JSON is kept as sent (MeliListingProspect.payload); fields_of reads what the Prospects tab shows.
A scrape of the same page again (organization + Mercado Libre's id) replaces the payload and keeps what the
user did with it: its status, product and draft.
"""

import logging
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import MeliListingProspect

logger = logging.getLogger(__name__)

# MLAU3250631132 (a user product page, /up/…) or MLA1234567 / MLA-1234567 (an item page).
MELI_ID = re.compile(r"\b(ML[A-Z]U?)-?(\d{6,})\b")
CATEGORY = re.compile(r"ML[A-Z]\d+")
LIMIT = 100


class ProspectError(ValueError):
    """A scraped entry that can't be stored, with the reason the caller gets."""


def _text(value, limit):
    return " ".join(str(value or "").split())[:limit]


def _meli_id(entry):
    """The scraped listing's key: productId (or product_id) as sent, else a Mercado Libre id in id or the page's URL.

    A second listing with the same key replaces the stored JSON.
    """
    for key in ("productId", "product_id"):
        value = str(entry.get(key) or "").strip()
        if value:
            found = MELI_ID.fullmatch(value.upper())
            return found.group(1) + found.group(2) if found else value
    found = MELI_ID.fullmatch(str(entry.get("id") or "").strip().upper())
    if found:
        return found.group(1) + found.group(2)
    for key in ("pageUrl", "productUrl", "url", "permalink"):
        found = MELI_ID.search(str(entry.get(key) or "").upper())
        if found:
            return found.group(1) + found.group(2)
    return ""


def _price(value):
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        price = Decimal(str(value))
    except InvalidOperation:
        return None
    return price if price.is_finite() and 0 <= price < Decimal("1e12") else None


def _when(value):
    if not value:
        return None
    try:
        parsed = parse_datetime(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if isinstance(parsed, datetime) and parsed.tzinfo else None


def fields_of(entry):
    """The columns the Prospects tab shows, read from one scraped entry. Raises ProspectError without an id."""
    if not isinstance(entry, dict):
        raise ProspectError("Each listing must be a JSON object.")
    external_id = _meli_id(entry)
    if not external_id:
        raise ProspectError("No product id: send productId (MLAU… or MLA…), or id, or the page's pageUrl.")
    if len(external_id) > 32:
        raise ProspectError("productId is longer than 32 characters.")
    images = entry.get("images") if isinstance(entry.get("images"), dict) else {}
    gallery = images.get("gallery") if isinstance(images.get("gallery"), list) else []
    return {
        "external_id": external_id,
        "title": _text(entry.get("title"), 255),
        "price": _price(entry.get("currentPrice", entry.get("price"))),
        "url": str(entry.get("pageUrl") or entry.get("productUrl") or entry.get("permalink") or "")[:1024],
        "picture": str(next((url for url in gallery if url), "") or entry.get("thumbnail") or "")[:1024],
        "seller": _text(entry.get("seller"), 128),
        "scraped_at": _when(entry.get("scrapedAt")),
    }


def category_in(entry):
    """The category the scraper read from the page (categoryId, category_id or category.id), or ""."""
    category = entry.get("category")
    for value in (entry.get("categoryId"), entry.get("category_id"), category.get("id") if isinstance(category, dict) else category):
        value = str(value or "").strip().upper()
        if CATEGORY.fullmatch(value):
            return value
    return ""


def store(organization, entries):
    """Stores scraped listings for one organization: [(prospect, created)], in the order sent.

    Reads every entry before writing any, so one bad entry stores nothing. Repeats of one id in a single
    call keep the last.
    """
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list) or not entries:
        raise ProspectError("Send one scraped listing (a JSON object) or a list of them.")
    if len(entries) > LIMIT:
        raise ProspectError(f"Send at most {LIMIT} listings per call.")
    rows = []
    for index, entry in enumerate(entries):
        try:
            rows.append((fields_of(entry), entry))
        except ProspectError as error:
            raise ProspectError(f"Listing {index}: {error}") from None
    results = []
    with transaction.atomic():
        existing = {
            prospect.external_id: prospect
            for prospect in MeliListingProspect.objects.select_for_update().filter(
                organization=organization, external_id__in=[fields["external_id"] for fields, _ in rows]
            )
        }
        for fields, entry in rows:
            prospect = existing.get(fields["external_id"])
            created = prospect is None
            if created:
                prospect = MeliListingProspect(organization=organization)
            category = category_in(entry)
            if created or prospect.title != fields["title"] or (category and category != prospect.category_id):
                # Looked up again in the worker: the scraper's category, else a guess from the new title.
                prospect.category_id, prospect.category_path = category, ""
                prospect.category_checked_at, prospect.category_error = None, ""
            for name, value in fields.items():
                setattr(prospect, name, value)
            # The price's date: the scraper's scrapedAt, else when it reached Vimple.
            prospect.scraped_at = prospect.scraped_at if fields["scraped_at"] else timezone.now()
            prospect.payload = entry
            prospect.save()
            existing[prospect.external_id] = prospect
            results.append((prospect, created))
        transaction.on_commit(lambda: request_categories(organization, [prospect for prospect, _ in results]))
    return results


def request_categories(organization, prospects):
    """Queue the category lookup for those not looked up yet. One job; pages never call Mercado Libre."""
    from .jobs import dispatch_drafts_job

    ids = sorted({prospect.pk for prospect in prospects if prospect.category_checked_at is None})
    if ids:
        dispatch_drafts_job(organization, "prospect_categories", prospect_ids=ids)


def find_categories(organization, prospect_ids):
    """The worker's half: each scraped listing's category and its path. A failure stays on its row."""
    from django.utils import timezone

    from .client import describe_failure, fetch_category, predict_category
    from .drafts import _reason, _site, _token

    rows = list(
        MeliListingProspect.objects.filter(organization=organization, pk__in=list(prospect_ids), category_checked_at=None)
    )
    if not rows:
        return
    try:
        token, site = _token(organization), _site(organization)
    except Exception as error:
        logger.warning("Scraped listing categories of %s not found: %s", organization.pk, describe_failure(error))
        MeliListingProspect.objects.filter(pk__in=[row.pk for row in rows]).update(
            category_checked_at=timezone.now(), category_error=f"Couldn't look it up: {_reason(error)}"[:300]
        )
        return
    for row in rows:
        row.category_error = ""
        try:
            if not row.category_id and row.title:
                guess = next(
                    (item for item in predict_category(token, site, row.title) if isinstance(item, dict) and item.get("category_id")),
                    {},
                )
                row.category_id = str(guess.get("category_id") or "")[:32]
            if row.category_id:
                category = fetch_category(token, row.category_id)
                path = [str(step.get("name") or "") for step in category.get("path_from_root") or [] if isinstance(step, dict)]
                row.category_path = " > ".join(name for name in path if name)[:500] or str(category.get("name") or "")[:500]
            else:
                row.category_error = "Mercado Libre didn't suggest a category for this title."
        except Exception as error:
            logger.warning("Category of scraped listing %s not found: %s", row.external_id, describe_failure(error))
            row.category_error = f"Couldn't look it up: {_reason(error)}"[:300]
        row.category_checked_at = timezone.now()
        row.save(update_fields=["category_id", "category_path", "category_checked_at", "category_error", "updated_at"])



def link_listing(organization, item_id, sku):
    """The worker's half of linking a fast listing to a product: its SKU on Mercado Libre, then the item read
    again, so the sync links it by SKU and applies its waiting sales. A refusal shows on its Listings row."""
    from .client import describe_failure, update_item
    from .drafts import _reason, _token
    from .listings import refresh_listing
    from .models import MeliListing

    try:
        token = _token(organization)
        update_item(token, item_id, {"attributes": [{"id": "SELLER_SKU", "value_name": sku}]})
        refresh_listing(organization, item_id)
    except Exception as error:
        logger.warning("Listing %s not linked to %s: %s", item_id, sku, describe_failure(error))
        MeliListing.objects.filter(organization=organization, item_id=item_id).update(
            action_error=f"Couldn't set SKU {sku}: {_reason(error)}"[:300]
        )
        return
    logger.info("Listing %s linked to %s", item_id, sku)
