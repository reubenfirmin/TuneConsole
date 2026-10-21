"""The enabled slice of the shared genre catalog, used by metadata tag providers."""
import functools

from yt_playlist.util import genre_catalog

_active = None


def builtin_names():
    return genre_catalog.builtin_names()


def canonical_name(name):
    return genre_catalog.canonical_name(name)


def build_canon(names):
    records, labels = genre_catalog.catalog()
    enabled, custom = {}, {}
    for name in names:
        key = genre_catalog.normalize(name)
        if not key:
            continue
        if label := labels.get(key):
            enabled[label[0]] = records[label[0]]["name"]
        else:
            custom.setdefault(key, name)
    return {**{key: enabled[value[0]] for key, value in labels.items() if value[0] in enabled},
            **custom}


def set_active(names):
    global _active
    _active = build_canon(names) if names is not None else None


def configure(store):
    """Atomically upgrade the catalog and defaults, then publish the enabled matcher."""
    store.genres.configure_catalog()
    set_active(store.genres.get_genre_whitelist())


def reset(store):
    store.genres.configure_catalog(reset=True)
    set_active(store.genres.get_genre_whitelist())


@functools.lru_cache(maxsize=1)
def _builtin_canon():
    return build_canon(builtin_names())


def _canon():
    return _active if _active is not None else _builtin_canon()


def all_genres():
    return sorted(set(_canon().values()), key=str.casefold)


def match_tag(tag):
    return _canon().get(genre_catalog.normalize(tag))


def pick_genre(tags):
    """The first recognized genre from tags supplied in provider relevance order."""
    return next((genre for tag in tags if (genre := match_tag(tag))), None)
