"""Mercado Libre claims (reclamos, mediaciones, devoluciones) on this organization's sales.

A post_purchase webhook (/post-purchase/v1/claims/<id>, or …/actions-history) is read in the worker, once per
organization connected to the seller. Refresh (Claims tab) reads every open claim. The seller's deadline is the
earliest due_date among its available_actions: when it passes, Mercado Libre picks a resolution itself.
Nothing here answers or resolves a claim; the seller does that on Mercado Libre's sale page.
"""

import logging
import re
from datetime import timezone as dt_timezone

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .client import describe_failure, fetch_claim, fetch_open_claims, fetch_order
from .models import MeliClaim, MeliClaimSync, MeliListingSync

logger = logging.getLogger(__name__)

CLAIM_RESOURCE = re.compile(r"/post-purchase/v\d+/claims/(\d{1,30})(?:/[a-z_-]+)?")
# Words for Mercado Libre's action ids; any other id is shown as its words ("send_potential_shipping").
ACTION_LABELS = {
    "send_message_to_complainant": "Message the buyer",
    "send_message_to_mediator": "Message Mercado Libre",
    "refund": "Refund",
    "allow_return": "Accept the return",
    "allow_partial_refund": "Offer a partial refund",
    "open_dispute": "Ask Mercado Libre to step in",
    "send_attachments": "Send evidence",
    "add_shipping_evidence": "Add shipping evidence",
    "send_tracking_number": "Send the tracking number",
    "return_review_ok": "Confirm the return arrived fine",
    "return_review_fail": "Report a problem with the return",
}
TYPE_LABELS = {
    "mediations": "Mediation",
    "returns": "Return",
    "fulfillment": "Full",
    "cancel_purchase": "Cancellation",
    "cancel_sale": "Cancellation",
    "change": "Exchange",
    "service": "Service",
}
STAGE_LABELS = {"claim": "Claim", "dispute": "Mercado Libre stepped in", "recontact": "Reopened", "none": ""}


def action_label(action):
    action = str(action or "")
    return ACTION_LABELS.get(action, action.replace("_", " ").capitalize())


def _when(value):
    if not value:
        return None
    try:
        parsed = parse_datetime(str(value))
    except ValueError:
        return None
    if parsed is not None and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, dt_timezone.utc)
    return parsed


def _seller_player(claim, seller_id):
    """The seller's entry among the claim's players: by user id, else the respondent seller."""
    players = [player for player in claim.get("players") or [] if isinstance(player, dict)]
    for player in players:
        if seller_id and str(player.get("user_id") or "") == str(seller_id):
            return player
    for player in players:
        if player.get("type") == "seller" or player.get("role") == "respondent":
            return player
    return {}


def seller_actions(claim, seller_id):
    """[{action, mandatory, due_date}] the seller can take now, the earliest deadline first."""
    actions = []
    for item in _seller_player(claim, seller_id).get("available_actions") or []:
        if not isinstance(item, dict) or not item.get("action"):
            continue
        actions.append({
            "action": str(item["action"])[:64],
            "mandatory": bool(item.get("mandatory")),
            "due_date": str(item.get("due_date") or "")[:40],
        })
    actions.sort(key=lambda item: (_when(item["due_date"]) is None, _when(item["due_date"]) or timezone.now()))
    return actions


def store_claim(organization, claim, seller_id):
    """Upsert one claim read from Mercado Libre. Returns the row, or None when the answer has no id."""
    claim_id = str(claim.get("id") or "").strip()
    if not claim_id.isdigit():
        return None
    actions = seller_actions(claim, seller_id)
    dues = [_when(item["due_date"]) for item in actions if _when(item["due_date"])]
    status = str(claim.get("status") or "")[:16]
    values = {
        "resource": str(claim.get("resource") or "")[:32],
        "resource_id": str(claim.get("resource_id") or "")[:32],
        "status": status,
        "claim_type": str(claim.get("type") or "")[:32],
        "stage": str(claim.get("stage") or "")[:32],
        "reason_id": str(claim.get("reason_id") or "")[:32],
        "actions": actions,
        # A closed claim has nothing left to answer, whatever its old actions said.
        "due_at": min(dues) if dues and status == "opened" else None,
        "opened_at": _when(claim.get("date_created")),
        "meli_updated_at": _when(claim.get("last_updated")),
        "payload": claim,
        "fetched_at": timezone.now(),
    }
    row, _ = MeliClaim.objects.update_or_create(organization=organization, claim_id=claim_id, defaults=values)
    return row


def _add_sale(row, access_token):
    """The sold item's title and the buyer's nickname, read from the order once."""
    if row.title or row.resource != "order" or not row.resource_id:
        return
    try:
        order = fetch_order(access_token, row.resource_id)
    except Exception as error:
        logger.warning("Order %s of claim %s not read: %s", row.resource_id, row.claim_id, describe_failure(error))
        return
    items = [line for line in order.get("order_items") or [] if isinstance(line, dict)]
    item = items[0].get("item") if items and isinstance(items[0].get("item"), dict) else {}
    buyer = order.get("buyer") if isinstance(order.get("buyer"), dict) else {}
    row.title = str(item.get("title") or "")[:255]
    row.buyer = str(buyer.get("nickname") or "")[:128]
    row.save(update_fields=["title", "buyer"])


def load_claim_from_notification(job):
    """A post_purchase webhook: read the claim once per organization connected to this seller."""
    from .postsale import _account_for
    from .views import _clip, _organizations_for

    match = CLAIM_RESOURCE.fullmatch(str(job.get("resource") or ""))
    if not match:
        return 0
    stored = 0
    for organization in _organizations_for(_clip(job.get("user_id"), 64)):
        account = _account_for(organization)
        if account is None:
            continue
        try:
            token = account.fresh_access_token()
            row = store_claim(organization, fetch_claim(token, match.group(1)), account.meli_user_id)
        except Exception as error:
            logger.warning("Claim %s could not be loaded for %s: %s", match.group(1), organization.pk, describe_failure(error))
            continue
        if row is not None:
            _add_sale(row, token)
            stored += 1
    return stored


def sync_claims(organization):
    """Every open claim the seller must answer. A claim stored as open that isn't among them is read again."""
    from .listings import _reason
    from .postsale import _account_for

    record, _ = MeliClaimSync.objects.get_or_create(organization=organization)
    account = _account_for(organization)
    if account is None:
        record.state, record.error = MeliListingSync.State.FAILED, "Mercado Libre isn't connected."
        record.finished_at = timezone.now()
        record.save(update_fields=["state", "error", "finished_at", "updated_at"])
        return record
    record.state = MeliListingSync.State.RUNNING
    record.save(update_fields=["state", "updated_at"])
    try:
        token = account.fresh_access_token()
        found = fetch_open_claims(token, account.meli_user_id)
        rows = [row for row in (store_claim(organization, claim, account.meli_user_id) for claim in found) if row]
        seen = {row.claim_id for row in rows}
        # Closed since: the search no longer lists it, so its own answer says how it ended.
        for stale in MeliClaim.objects.filter(organization=organization, status="opened").exclude(claim_id__in=seen):
            try:
                rows.append(store_claim(organization, fetch_claim(token, stale.claim_id), account.meli_user_id) or stale)
            except Exception as error:
                # One claim that can't be read again stays as it was; the others still refresh.
                logger.warning("Claim %s not read again for %s: %s", stale.claim_id, organization.pk, describe_failure(error))
        for row in rows:
            _add_sale(row, token)
    except Exception as error:
        logger.warning("Claims sync failed for %s: %s", organization.pk, describe_failure(error))
        record.state, record.error = MeliListingSync.State.FAILED, _reason(error)[:300]
        record.finished_at = timezone.now()
        record.save(update_fields=["state", "error", "finished_at", "updated_at"])
        return record
    record.state, record.error, record.total = MeliListingSync.State.OK, "", len(seen)
    record.finished_at = timezone.now()
    record.save(update_fields=["state", "error", "total", "finished_at", "updated_at"])
    return record


def dispatch_claims_sync(organization):
    """Queue a Refresh on the drafts queue (never the reply lane). One already queued or running stays."""
    from .jobs import dispatch_drafts_job

    record, _ = MeliClaimSync.objects.get_or_create(organization=organization)
    if record.busy():
        return record
    record.state, record.error = MeliListingSync.State.QUEUED, ""
    record.save(update_fields=["state", "error", "updated_at"])
    dispatch_drafts_job(organization, "claims_sync")
    record.refresh_from_db()
    return record


def due_summary(organization):
    """(open claims with a deadline, the earliest deadline): one query, for the bell and the sidebar."""
    from django.db.models import Count, Min

    if organization is None:
        return 0, None
    found = MeliClaim.objects.filter(organization=organization, status="opened", due_at__isnull=False).aggregate(
        count=Count("pk"), first=Min("due_at")
    )
    return found["count"], found["first"]
