"""Deleting from an edit page: one card, one confirmation, and the server re-checks what blocks it."""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import redirect


def counted(*parts):
    """ "2 purchase lines, 1 stock movement" from (number, singular, plural) parts; zeros are left out."""
    return ", ".join(f"{number} {singular if number == 1 else plural}" for number, singular, plural in parts if number)


def card(name, blocked="", consequences=(), archive=False):
    """What the page's Delete card shows: the thing, what goes with it, or why it can't go."""
    return {"name": name, "blocked": blocked, "consequences": [line for line in consequences if line], "archive": archive}


def delete(request, row, blocked, back, done, name):
    """Delete row unless something still needs it; back is where a refusal returns, done where a delete goes."""
    if blocked:
        messages.warning(request, f"{name} wasn't deleted: {blocked}")
        return redirect(back)
    row.delete()
    messages.success(request, f"{name} deleted.")
    return redirect(done)


def bulk(name, *actions, fields=None):
    """A list's selection bar: its form id and its buttons as (action, label, confirmation, danger).

    The confirmation's {n} becomes the number selected.
    """
    return {"id": f"bulk-{name}", "fields": sorted((fields or {}).items()), "actions": [
        {"value": value, "label": label, "confirm": confirm, "danger": danger}
        for value, label, confirm, danger in actions
    ]}


DELETE_ACTION = ("delete_selected", "Delete", "Delete {n} selected? This can't be undone.", True)


def selected(request, queryset):
    """The rows ticked in a list, read only from queryset (already this organization's)."""
    pk = queryset.model._meta.pk
    ids = []
    for value in request.POST.getlist("selected")[:500]:
        try:
            ids.append(pk.to_python(value))
        except ValidationError:
            continue  # Not an id of this list: ignored, the rest still count.
    return list(queryset.filter(pk__in=ids))


def delete_selected(request, queryset, blocked, label, back):
    """Delete the selected rows nothing else needs; the others stay, each with its reason."""
    deleted, kept = 0, []
    for row in selected(request, queryset):
        reason = blocked(row) if blocked else ""
        if reason:
            kept.append(f"{label(row)} ({reason})")
            continue
        row.delete()
        deleted += 1
    if deleted:
        messages.success(request, f"{deleted} deleted.")
    if kept:
        messages.warning(request, f"Not deleted: {'; '.join(kept[:10])}{' …' if len(kept) > 10 else ''}")
    return redirect(back)


ARCHIVE_ACTION = ("archive_selected", "Archive", "", False)


def archive_selected(request, queryset, back):
    """Archive the selected rows: they keep their history and leave lists and new listings."""
    rows = selected(request, queryset)
    count = queryset.filter(pk__in=[row.pk for row in rows]).update(status="archived")
    messages.success(request, f"{count} archived.")
    return redirect(back)
