"""Channel hooks. Mercado Libre and later platforms register here."""

_senders = {}
_reconcilers = []
_openers = []
_list_actions = []
_DEFAULT_REPLY_LIMIT = 2000


class ReplyFailed(Exception):
    """The channel rejected the reply, or the text cannot be sent."""


def register_sender(channel, send, max_length=_DEFAULT_REPLY_LIMIT):
    _senders[channel] = {"send": send, "max_length": max_length}


def sender_for(channel):
    row = _senders.get(channel)
    return row["send"] if row else None


def reply_limit_for(channel):
    row = _senders.get(channel)
    if row is None:
        return _DEFAULT_REPLY_LIMIT
    return row["max_length"]


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
