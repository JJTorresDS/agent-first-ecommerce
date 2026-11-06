import hashlib
import logging

from django.core.cache import cache
from django.shortcuts import render
from django.views.decorators.http import require_GET

from accounts.access import require_app
from accounts.models import AppAccess, membership_for

from .client import describe_failure, predict_category
from .models import MercadoLibreAccount

logger = logging.getLogger(__name__)
TOP = 2
CACHE_SECONDS = 10 * 60


@require_app(AppAccess.App.CATALOG)
@require_GET
def meli_categories(request):
    """Mercado Libre's top categories for a title, as suggestions for a category field (codes only).

    The answer is a <datalist id="category-suggestions">: value MLA1271, label "Perfumes (…)".
    Cached per site and title for 10 minutes, so typing doesn't call Mercado Libre each time.
    """
    from .drafts import _site, _token

    organization = membership_for(request.user).organization
    title = " ".join((request.GET.get("title") or request.GET.get("q") or "").split())[:200]
    options, note = [], ""
    if not title:
        note = "Type the title to get Mercado Libre's categories."
    elif not MercadoLibreAccount.objects.filter(organization=organization).exists():
        note = "Connect Mercado Libre to get its category suggestions."
    else:
        site = _site(organization)
        key = "meli:categories:" + hashlib.sha1(f"{site}:{title.lower()}".encode()).hexdigest()
        options = cache.get(key)
        if options is None:
            try:
                options = top_categories(predict_category(_token(organization), site, title))
            except Exception as error:
                logger.warning("Category suggestions failed: %s", describe_failure(error))
                options, note = [], "Mercado Libre didn't answer; type the code yourself."
            else:
                cache.set(key, options, CACHE_SECONDS)
        if not options and not note:
            note = "Mercado Libre has no category for this title; try a clearer one."
    return render(request, "meli/_category_options.html", {"options": options, "note": note})


def top_categories(guesses):
    """The first distinct categories: [{"id": "MLA1271", "label": "Perfumes (Belleza › Perfumes)"}]."""
    options, seen = [], set()
    for guess in guesses or []:
        if not isinstance(guess, dict) or not guess.get("category_id"):
            continue
        category = str(guess["category_id"])
        if category in seen:
            continue
        seen.add(category)
        name = str(guess.get("category_name") or "").strip()
        domain = str(guess.get("domain_name") or "").strip()
        label = name if not domain or domain == name else f"{name} ({domain})"
        options.append({"id": category, "label": label or category})
        if len(options) == TOP:
            break
    return options
