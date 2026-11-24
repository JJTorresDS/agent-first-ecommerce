"""POST /api/meli/prospects: scraped Mercado Libre listings, stored as pending prospects for one organization.

The body is the scraper's JSON as it is: one listing object, or a list of them. The caller sends the
organization's API key (accounts.OrganizationApiKey) as a Bearer token and names the organization; a key
only ever reaches its own.
"""

import json
import logging

from django.utils import timezone
from ninja import Router, Schema
from ninja.errors import HttpError

from accounts.models import OrganizationApiKey
from tasks.api import _organization_or_404

from .prospects import ProspectError, store
from .token_api import OrganizationKeyAuth

logger = logging.getLogger(__name__)

router = Router(tags=["Mercado Libre"])


class ProspectOut(Schema):
    id: int
    external_id: str
    status: str
    created: bool


class ProspectsOut(Schema):
    created: int
    updated: int
    prospects: list[ProspectOut]


@router.post(
    "/prospects",
    response={200: ProspectsOut, 201: ProspectsOut},
    auth=OrganizationKeyAuth(),
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": {"oneOf": [{"type": "object"}, {"type": "array", "items": {"type": "object"}}]}}},
        }
    },
)
def create_prospects(request, organization: str):
    """Stores scraped listings as prospects (status pending). A listing already stored is updated, keeping its status."""
    key = request.auth
    named = _organization_or_404(organization)
    if named.pk != key.organization_id:
        # Same answer as an unknown id: the key doesn't confirm another organization exists.
        raise HttpError(404, "Not Found")
    try:
        body = json.loads(request.body or b"null")
    except (ValueError, UnicodeDecodeError):
        raise HttpError(400, "The body must be JSON.") from None
    try:
        results = store(named, body)
    except ProspectError as error:
        raise HttpError(422, str(error)) from None
    OrganizationApiKey.objects.filter(pk=key.pk).update(last_used_at=timezone.now())
    created = sum(1 for _, new in results if new)
    logger.info("Prospects for %s: %s new, %s updated", named.pk, created, len(results) - created)
    return 201 if created else 200, {
        "created": created,
        "updated": len(results) - created,
        "prospects": [
            {"id": prospect.pk, "external_id": prospect.external_id, "status": prospect.status, "created": new}
            for prospect, new in results
        ],
    }
