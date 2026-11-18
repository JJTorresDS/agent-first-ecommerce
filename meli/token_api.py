"""GET /api/meli/token: an organization's Mercado Libre access token, for another service of the owner's.

The caller sends the organization's API key (accounts.OrganizationApiKey) as a Bearer token and names the
organization. A key only ever reaches its own organization. Vimple keeps refreshing the token itself
(refresh tokens work once and are shared across the seller's organizations), so the refresh token never leaves.
"""

import logging
from datetime import datetime

from cryptography.fernet import InvalidToken
from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Schema
from ninja.errors import HttpError
from ninja.security import HttpBearer

from accounts.models import OrganizationApiKey
from tasks.api import _organization_or_404

from .client import MercadoLibreError, describe_failure
from .models import MercadoLibreAccount

logger = logging.getLogger(__name__)


class OrganizationKeyAuth(HttpBearer):
    def authenticate(self, request: HttpRequest, token: str):
        return OrganizationApiKey.matching(token)


router = Router(tags=["Mercado Libre"])


class TokenOut(Schema):
    access_token: str
    token_type: str
    expires_at: datetime
    meli_user_id: str


@router.get("/token", response=TokenOut, auth=OrganizationKeyAuth())
def meli_token(request, organization: str):
    key = request.auth
    named = _organization_or_404(organization)
    if named.pk != key.organization_id:
        # Same answer as an unknown id: the key doesn't confirm another organization exists.
        raise HttpError(404, "Not Found")
    account = MercadoLibreAccount.objects.filter(organization_id=key.organization_id).first()
    if account is None:
        raise HttpError(404, "Mercado Libre isn't connected for this organization.")
    try:
        access_token = account.fresh_access_token()
    except (InvalidToken, MercadoLibreError, OSError, ValueError) as error:
        logger.warning("Token for %s not issued: %s", key.organization_id, describe_failure(error))
        reason = error.message if isinstance(error, MercadoLibreError) else describe_failure(error)
        raise HttpError(503, f"The Mercado Libre token isn't available: {reason}") from None
    OrganizationApiKey.objects.filter(pk=key.pk).update(last_used_at=timezone.now())
    logger.info("Mercado Libre token issued to the API key of %s", key.organization_id)
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_at": account.expires_at,
        "meli_user_id": account.meli_user_id,
    }
