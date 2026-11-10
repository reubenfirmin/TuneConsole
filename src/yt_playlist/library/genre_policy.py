"""Genre vocabulary, ambiguity and preference decisions over catalog facts."""
import json

from yt_playlist.util import genre_catalog


def validate_name(name, *, custom=False):
    if not isinstance(name, str) or not name.strip() or len(name.encode()) > 1024:
        subject = 'A custom genre' if custom else 'A genre'
        raise ValueError(subject + ' requires 1..1024 bytes of nonblank text')


def custom_key(name, resolved, reason):
    validate_name(name, custom=True)
    if reason == 'ambiguous_genre':
        raise ValueError('Ambiguous genre label: ' + name)
    if resolved is not None:
        return resolved
    key = genre_catalog.normalize(name)
    if not key:
        raise ValueError('A custom genre requires a nonempty identity')
    return key


def taxonomy(facts):
    aliases = {}
    redirects = facts['redirects']
    for key, label, source in facts['aliases']:
        if source != 'spelling':
            aliases.setdefault(redirects.get(key, key), set()).add(label)
    return {'families': facts['families'], 'genres': [
        {'key': key, 'name': name, 'family': family or 'other:' + key,
         'aliases': sorted(aliases.get(key, set()) - {name}, key=str.casefold)}
        for key, name, family in facts['genres'] if key not in redirects]}


def matcher(facts):
    if facts is None:
        return None
    matches = {}
    for label, key in facts['labels']:
        matches.setdefault(genre_catalog.normalize(label), set()).add(facts['redirects'].get(key, key))
    enabled = facts['enabled']
    return {label: enabled[next(iter(keys))] for label, keys in matches.items()
            if len(keys) == 1 and next(iter(keys)) in enabled}


def merge_preference(preferences):
    if not preferences:
        return None
    explicit = [row for row in preferences if row['origin'] in ('user', 'custom', 'legacy')]
    return min(row['enabled'] for row in explicit or preferences), 'user' if explicit else 'default'


def configuration(current, settings, *, reset=False):
    defaults = genre_catalog.builtin_names()
    upgrading = settings.get('genre_catalog_revision') != genre_catalog.REVISION
    if reset or not (settings.get('genres_seeded') or current):
        target = defaults
    elif upgrading and current:
        previous = settings.get('genre_defaults')
        baseline = json.loads(previous) if previous is not None else genre_catalog.legacy_names()
        previous_keys = {genre_catalog.normalize(genre_catalog.canonical_name(name)) for name in baseline}
        active_keys = {genre_catalog.normalize(genre_catalog.canonical_name(name)) for name in current}
        blocked = set()
        if previous is None:
            for name, aliases in genre_catalog.legacy_entries():
                if genre_catalog.normalize(genre_catalog.canonical_name(name)) not in active_keys:
                    blocked.update(genre_catalog.normalize(genre_catalog.canonical_name(alias))
                                   for alias in [name, *aliases])
        target = [*current, *(name for name in defaults
                              if genre_catalog.normalize(name) not in previous_keys | blocked)]
    else:
        target = current
    canonical = {}
    for name in target:
        name = genre_catalog.canonical_name(name)
        canonical.setdefault(genre_catalog.normalize(name), name)
    return sorted(canonical.values(), key=str.casefold), canonical, defaults, upgrading
