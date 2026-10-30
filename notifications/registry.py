_providers = []


def register(provider):
    if provider not in _providers:
        _providers.append(provider)


def groups_for(organization):
    if organization is None:
        return []
    groups = []
    for provider in _providers:
        group = provider(organization)
        if group and group.get("count"):
            groups.append(group)
    return groups
