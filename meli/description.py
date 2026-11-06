"""A regular listing's description: the product's own text and saved copy blocks, in the order the seller put them.

ListingDraft.description_parts holds the order. Blocks are read live until the draft is posted;
posted_description keeps what was sent.
"""

import re

from django.db.models import Q

from catalog.models import Combo, CopyBlock

from .drafts import description_of

LIMIT = 50000
TEXT_LIMIT = 5000
SEPARATOR = "\n\n"
EMPTY = "Write a description first: it's empty."
_LINK = re.compile(r"(https?://|www\.)\S+|\b[\w-]+\.(com|ar|net|org)(\.ar)?\b", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE = re.compile(r"(?<!\d)(\+?\d[\d\s().-]{7,}\d)(?!\d)")


def category_of(listed):
    """The Vimple category whose copy fits: the product's, or the one all of a combo's products share."""
    if isinstance(listed, Combo):
        categories = {item.product.product_model.category_id for item in listed.items.select_related("product__product_model")}
        return categories.pop() if len(categories) == 1 else None
    model = getattr(listed, "product_model", None)
    return model.category_id if model else None


def blocks_for(organization, listed):
    """The copy offered for this listing: blocks for every listing, then its category's, in layout order."""
    category = category_of(listed)
    rows = CopyBlock.objects.filter(organization=organization).filter(
        Q(category__isnull=True) | Q(category_id=category)
    ).select_related("category")
    return sorted(rows, key=CopyBlock.kind_rank)


def default_parts(organization, listed):
    """A new draft's description: the product's own text, then the blocks marked "Add to new listings"."""
    return [{"product": True}] + [{"block": row.pk} for row in blocks_for(organization, listed) if row.default_on]


def clean_parts(organization, parts):
    """Only well-formed pieces, and only this organization's blocks."""
    ids = {part.get("block") for part in parts if isinstance(part, dict)}
    known = set(CopyBlock.objects.filter(organization=organization, pk__in=[i for i in ids if isinstance(i, int)]).values_list("pk", flat=True))
    cleaned = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("product") is True:
            cleaned.append({"product": True})
        elif part.get("block") in known:
            cleaned.append({"block": part["block"]})
        elif isinstance(part.get("text"), str) and part["text"].strip():
            cleaned.append({"text": part["text"].strip()[:TEXT_LIMIT]})
    return cleaned


def parts_from(post, organization):
    """The composer's pieces as posted: desc_part in order, desc_text for each text piece."""
    texts = iter(post.getlist("desc_text"))
    parts = []
    for value in post.getlist("desc_part"):
        if value == "product":
            parts.append({"product": True})
        elif value.startswith("block:") and value[6:].isdigit():
            parts.append({"block": int(value[6:])})
        elif value == "text":
            parts.append({"text": next(texts, "")})
    return clean_parts(organization, parts)


def compose(draft, blocks=None):
    """The text posted: each piece in order, a blank line between. Blocks of another organization never count."""
    if not draft.description_parts:
        return description_of(draft.listed())
    if blocks is None:
        ids = [part["block"] for part in draft.description_parts if "block" in part]
        blocks = {row.pk: row for row in CopyBlock.objects.filter(organization_id=draft.organization_id, pk__in=ids)}
    pieces = []
    for part in draft.description_parts:
        if part.get("product"):
            pieces.append(description_of(draft.listed()))
        elif "block" in part and part["block"] in blocks:
            pieces.append(blocks[part["block"]].body)
        elif "text" in part:
            pieces.append(part["text"])
    return SEPARATOR.join(piece.strip() for piece in pieces if piece and piece.strip())[:LIMIT]


def warnings(text):
    """What Mercado Libre moderates or refuses in a description. Only an empty one stops the post."""
    found = []
    if not text.strip():
        found.append(EMPTY)
    if _LINK.search(text):
        found.append("It has a link: Mercado Libre doesn't allow links in descriptions.")
    if _EMAIL.search(text):
        found.append("It has an email address: Mercado Libre moderates contact details.")
    if _PHONE.search(text):
        found.append("It looks like it has a phone number: Mercado Libre moderates contact details.")
    return found
