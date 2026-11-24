"""Prospect listings: Mercado Libre listings someone else sells, scraped and sent to POST /api/meli/prospects.

The scraper's JSON is kept as sent (MeliListingProspect.payload); fields_of reads what the Prospects tab shows.
A scrape of the same page again (organization + Mercado Libre's id) replaces the payload and keeps what the
user did with it: its status, product and draft.
"""

import re
import unicodedata
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils.dateparse import parse_datetime

from .models import MeliListingProspect

# MLAU3250631132 (a user product page, /up/…) or MLA1234567 / MLA-1234567 (an item page).
MELI_ID = re.compile(r"\b(ML[A-Z]U?)-?(\d{6,})\b")
LIMIT = 100


class ProspectError(ValueError):
    """A scraped entry that can't be stored, with the reason the caller gets."""


def _text(value, limit):
    return " ".join(str(value or "").split())[:limit]


def _meli_id(entry):
    for key in ("id", "productId"):
        found = MELI_ID.fullmatch(str(entry.get(key) or "").strip().upper())
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
        raise ProspectError("No Mercado Libre id: send id (MLAU… or MLA…) or the page's pageUrl.")
    images = entry.get("images") if isinstance(entry.get("images"), dict) else {}
    gallery = images.get("gallery") if isinstance(images.get("gallery"), list) else []
    return {
        "external_id": external_id[:32],
        "title": _text(entry.get("title"), 255),
        "price": _price(entry.get("currentPrice", entry.get("price"))),
        "url": str(entry.get("pageUrl") or entry.get("productUrl") or entry.get("permalink") or "")[:1024],
        "picture": str(next((url for url in gallery if url), "") or entry.get("thumbnail") or "")[:1024],
        "seller": _text(entry.get("seller"), 128),
        "scraped_at": _when(entry.get("scrapedAt")),
    }


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
            for name, value in fields.items():
                setattr(prospect, name, value)
            prospect.payload = entry
            prospect.save()
            existing[prospect.external_id] = prospect
            results.append((prospect, created))
    return results


def _name(value):
    """ "Línea" and "linea" match: Mercado Libre's attribute names as the page shows them."""
    plain = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
    return " ".join(plain.lower().split())


def offered_from(prospect, schema):
    """The prospect's characteristics as [(attribute id, name, value)], matched by name to the category's attributes.

    The page shows each attribute under the name the category schema has ("Color", "Material"), so the name
    is the link; characteristics with no attribute of that name in the category are left out.
    """
    by_name = {}
    for item in schema.values():
        by_name.setdefault(_name(item.get("name") or ""), item)
    offered = []
    for name, value in prospect.characteristics().items():
        item = by_name.get(_name(name))
        if item:
            offered.append((item["id"], str(item.get("name") or name)[:100], " ".join(value.split())[:200]))
    return offered
