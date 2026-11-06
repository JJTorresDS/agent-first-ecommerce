"""Template listings: one per category, real Mercado Libre items kept paused so they never sell.

New drafts in the category copy their attributes from it. Vimple never activates one, and pauses it
again when the sync or a webhook finds it active.
"""

import logging

from django.utils import timezone

from .client import describe_failure, pause_item
from .models import ListingTemplate, MeliListing

logger = logging.getLogger(__name__)

LIVE = "The template is live on Mercado Libre: pause it there now."


def _reason(error):
    from .drafts import _reason

    return _reason(error)


def _token(organization):
    from .drafts import _token

    return _token(organization)


def _paused(organization, item_id):
    MeliListing.objects.filter(organization=organization, item_id=item_id).update(status="paused")


def mark(organization, listing):
    """Make this listing its category's template. It's paused in the worker before it counts as one."""
    from .jobs import dispatch_drafts_job

    row, _ = ListingTemplate.objects.update_or_create(
        organization=organization,
        category_id=listing.category_id,
        defaults={"item_id": listing.item_id, "title": listing.title, "draft": None,
                  "state": ListingTemplate.State.PAUSING, "error": "", "note": ""},
    )
    dispatch_drafts_job(organization, "template", item_id=listing.item_id)
    return row


def pause_marked(organization, item_id):
    """The worker's half of mark: pause the item, then it's the template; a refusal is shown, not hidden."""
    row = ListingTemplate.objects.filter(organization=organization, item_id=item_id).first()
    if row is None:
        return
    try:
        pause_item(_token(organization), item_id)
    except Exception as error:
        logger.warning("Template %s was not paused: %s", item_id, describe_failure(error))
        row.state = ListingTemplate.State.FAILED
        row.error = f"Not paused, so not a template: {_reason(error)}"[:500]
        row.save(update_fields=["state", "error", "updated_at"])
        return
    _paused(organization, item_id)
    row.state = ListingTemplate.State.READY
    row.error = ""
    row.save(update_fields=["state", "error", "updated_at"])


def posted(organization, draft, token):
    """A template draft was just posted: pause it at once. Returns a note when it couldn't be paused."""
    row, _ = ListingTemplate.objects.update_or_create(
        organization=organization,
        category_id=draft.category_id,
        defaults={"item_id": draft.item_id, "title": draft.listed().title[:255], "draft": draft,
                  "state": ListingTemplate.State.PAUSING, "error": "", "note": ""},
    )
    try:
        pause_item(token, draft.item_id)
    except Exception as error:
        logger.warning("Template %s was posted but not paused: %s", draft.item_id, describe_failure(error))
        row.state = ListingTemplate.State.FAILED
        row.error = f"{LIVE} {_reason(error)}"[:500]
        row.save(update_fields=["state", "error", "updated_at"])
        return row.error
    row.state = ListingTemplate.State.READY
    row.save(update_fields=["state", "updated_at"])
    return ""


def pause_active(organization, items):
    """Templates the sync or a webhook found active are paused again: Vimple never sells a template."""
    active = {
        str(item.get("id"))
        for item in items.values()
        if item and item.get("status") == "active"
    }
    if not active:
        return
    rows = list(ListingTemplate.objects.filter(organization=organization, item_id__in=active))
    if not rows:
        return
    try:
        token = _token(organization)
    except Exception as error:
        logger.warning("Active templates of %s not paused: %s", organization.pk, describe_failure(error))
        return
    for row in rows:
        try:
            pause_item(token, row.item_id)
        except Exception as error:
            logger.warning("Active template %s not paused: %s", row.item_id, describe_failure(error))
            row.state = ListingTemplate.State.FAILED
            row.error = f"{LIVE} {_reason(error)}"[:500]
            row.save(update_fields=["state", "error", "updated_at"])
            continue
        logger.info("Template %s was active on Mercado Libre; paused again", row.item_id)
        _paused(organization, row.item_id)
        row.state = ListingTemplate.State.READY
        row.error = ""
        row.note = f"Reactivated on Mercado Libre; paused again {timezone.localtime():%d/%m %H:%M}."
        row.save(update_fields=["state", "error", "note", "updated_at"])
