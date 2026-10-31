"""Channel hooks. Mercado Libre and later platforms register here."""

_senders = {}
_reconcilers = []
_openers = []
_list_actions = []


class ReplyFailed(Exception):
    """The channel rejected the reply, or the text cannot be sent."""


def register_sender(channel, send):
    _senders[channel] = send


def sender_for(channel):
    return _senders.get(channel)


def register_reconciler(reconcile):
    if reconcile not in _reconcilers:
        _reconcilers.append(reconcile)


def register_opener(opener):
    if opener not in _openers:
        _openers.append(opener)


def open_inbox(organization):
    """Heal channel-specific gaps, then project local rows into the inbox."""
    if organization is None:
        return
    for opener in _openers:
        opener(organization)
    reconcile(organization)


def reconcile(organization):
    if organization is None:
        return
    for reconcile_organization in _reconcilers:
        reconcile_organization(organization)


def register_list_action(action):
    if action not in _list_actions:
        _list_actions.append(action)


def list_actions(organization):
    if organization is None:
        return []
    actions = []
    for action in _list_actions:
        row = action(organization)
        if row:
            actions.append(row)
    return actions
