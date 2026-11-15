"""Catalog competition: the winning price of a catalog page, read only when a user asks.

GET /items/<id>/price_to_win, one call per catalog listing, in the worker (drafts queue, action catalog_prices).
Never on a schedule. The answer lands in CatalogCompetition, by item_id.
"""

import logging

from django.utils import timezone

from .client import describe_failure
from .listings import _money
from .models import CatalogCompetition, MeliListing

logger = logging.getLogger(__name__)


def checkable(organization):
    """This organization's catalog listings worth asking about: every one but the closed."""
    return sorted(set(
        MeliListing.objects.filter(organization=organization, catalog_listing=True)
        .exclude(status="closed")
        .values_list("item_id", flat=True)
    ))


def request_check(organization, item_ids):
    """Mark these items checking (their rows poll) and queue one job. Only this organization's catalog listings."""
    from .jobs import dispatch_drafts_job

    allowed = set(checkable(organization))
    item_ids = sorted({str(item) for item in item_ids} & allowed)
    if not item_ids:
        return []
    now = timezone.now()
    for item_id in item_ids:
        CatalogCompetition.objects.update_or_create(
            organization=organization,
            item_id=item_id,
            defaults={"state": CatalogCompetition.State.CHECKING, "requested_at": now, "error": ""},
        )
    dispatch_drafts_job(organization, "catalog_prices", item_ids=item_ids)
    return item_ids


def _winner(payload):
    winner = payload.get("winner")
    return winner if isinstance(winner, dict) else {}


def store(row, payload):
    winner = _winner(payload)
    row.state = CatalogCompetition.State.READY
    row.status = str(payload.get("status") or "")[:32]
    row.currency = str(payload.get("currency_id") or winner.get("currency_id") or "")[:8]
    row.current_price = _money(payload.get("current_price"))
    row.price_to_win = _money(payload.get("price_to_win"))
    row.winner_item_id = str(winner.get("item_id") or "")[:32]
    row.winner_price = _money(winner.get("price"))
    row.reasons = [str(reason) for reason in payload.get("reason") or [] if reason][:10]
    row.error = ""
    row.payload = payload
    row.checked_at = timezone.now()
    row.save()


def check(organization, item_ids):
    """The worker's half: ask Mercado Libre for each item; a failure is kept on its row, the rest go ahead."""
    from .client import fetch_price_to_win
    from .listing_templates import _reason, _token

    rows = {
        row.item_id: row
        for row in CatalogCompetition.objects.filter(organization=organization, item_id__in=list(item_ids))
    }
    if not rows:
        return
    try:
        token = _token(organization)
    except Exception as error:
        logger.warning("Catalog prices of %s not read: %s", organization.pk, describe_failure(error))
        for row in rows.values():
            row.state, row.error = CatalogCompetition.State.FAILED, f"Couldn't check: {_reason(error)}"[:300]
            row.save(update_fields=["state", "error"])
        return
    for item_id, row in rows.items():
        try:
            payload = fetch_price_to_win(token, item_id)
        except Exception as error:
            logger.warning("Catalog price of %s not read: %s", item_id, describe_failure(error))
            row.state, row.error = CatalogCompetition.State.FAILED, f"Couldn't check: {_reason(error)}"[:300]
            row.save(update_fields=["state", "error"])
            continue
        store(row, payload)
