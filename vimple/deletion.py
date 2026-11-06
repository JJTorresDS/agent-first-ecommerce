"""Deleting from an edit page: one card, one confirmation, and the server re-checks what blocks it."""

from django.contrib import messages
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
