_providers = []


def register(provider):
    if provider not in _providers:
        _providers.append(provider)


def groups_for(organization, waiting=None):
    """waiting is {channel: chats waiting} for this organization when the caller already counted them."""
    if organization is None:
        return []
    groups = []
    for provider in _providers:
        group = provider(organization, waiting)
        if group and group.get("count"):
            groups.append(group)
    return groups
