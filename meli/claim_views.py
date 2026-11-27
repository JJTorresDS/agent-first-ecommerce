"""Claims tab (Mercado Libre section): this organization's claims, the nearest deadline first."""

from django.contrib.auth.decorators import login_required
from django.db.models import F
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone

from accounts.models import membership_for

from .claims import STAGE_LABELS, TYPE_LABELS, action_label, dispatch_claims_sync
from .models import BUENOS_AIRES, MeliClaim, MeliClaimSync, MercadoLibreAccount

CLOSED_LIMIT = 100
URGENT_HOURS = 24


def time_left(due_at, now=None):
    """ "in 5 h", "in 2 days", "Overdue": how long the seller has left."""
    seconds = (due_at - (now or timezone.now())).total_seconds()
    if seconds <= 0:
        return "Overdue"
    hours = seconds / 3600
    if hours < 1:
        return f"in {max(1, int(seconds // 60))} min"
    if hours < 48:
        return f"in {int(hours)} h"
    return f"in {int(hours // 24)} days"


def deadline_text(due_at):
    return due_at.astimezone(BUENOS_AIRES).strftime("%a %d %b %H:%M")


@login_required
def mercadolibre_claims(request):
    membership = membership_for(request.user)
    if membership is None:
        return redirect("organization_setup")
    organization = membership.organization
    connected = MercadoLibreAccount.objects.filter(organization=organization).exists()
    if request.method == "POST":
        if connected:
            dispatch_claims_sync(organization)
        return redirect(request.path)
    record = MeliClaimSync.objects.filter(organization=organization).first()
    if request.GET.get("poll") == "sync" and request.headers.get("HX-Request") == "true":
        response = HttpResponse(status=204)
        if record is None or not record.busy():
            # Done: the table reads the claims again.
            response["HX-Refresh"] = "true"
        return response
    if connected and (record is None or (record.finished_at is None and not record.busy())):
        # First visit: claims from before Vimple listened, without waiting for a click.
        record = dispatch_claims_sync(organization)
    showing = "closed" if request.GET.get("status") == "closed" else "open"
    claims = MeliClaim.objects.filter(organization=organization)
    if showing == "open":
        claims = claims.filter(status="opened").order_by(F("due_at").asc(nulls_last=True), "-opened_at")
    else:
        claims = claims.exclude(status="opened").order_by("-meli_updated_at", "-pk")[:CLOSED_LIMIT]
    now = timezone.now()
    rows = list(claims)
    for row in rows:
        row.kind = TYPE_LABELS.get(row.claim_type, row.claim_type.replace("_", " ").capitalize())
        row.stage_label = STAGE_LABELS.get(row.stage, row.stage.replace("_", " ").capitalize())
        row.action_labels = list(dict.fromkeys(action_label(item.get("action")) for item in row.actions or []))
        if row.due_at:
            row.deadline = deadline_text(row.due_at)
            row.left = time_left(row.due_at, now)
            row.urgent = (row.due_at - now).total_seconds() < URGENT_HOURS * 3600
    open_count = MeliClaim.objects.filter(organization=organization, status="opened").count() if showing == "closed" else len(rows)
    return render(request, "meli/claims.html", {
        "claims": rows,
        "showing": showing,
        "open_count": open_count,
        "connected": connected,
        "record": record,
        "active_tab": "claims",
    })
