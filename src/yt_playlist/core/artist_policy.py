"""Artist references and name ambiguity, independent of catalog storage."""
from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.util.matching import normalize


def token(identity):
    return f'@artist:{identity}'


def is_reference(value):
    return isinstance(value, int) or str(value).startswith('@artist:')


def reference_id(value):
    try:
        return int(str(value).removeprefix('@artist:'))
    except ValueError:
        return None


def unique(identities):
    return next(iter(identities)) if len(identities) == 1 else None


def normalized_matches(aliases, names):
    wanted = {normalize(name) for name in names}
    matches = {name: set() for name in wanted}
    for index, row in enumerate(aliases):
        if index % 64 == 0:
            check_cancellable_work()
        name = normalize(row['alias'])
        if name in matches:
            matches[name].add(row['artist_id'])
    return {name: matches[normalize(name)] for name in names}


def public_keys(names, aliases):
    matches = normalized_matches(aliases, names.values())
    return {identity: normalize(name) if len(matches[name]) == 1 else token(identity)
            for identity, name in names.items()}
