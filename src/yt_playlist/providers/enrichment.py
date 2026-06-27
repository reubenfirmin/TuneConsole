"""Enrichment provider config: the ordered, enable/disable list the Setup → Enrichment tab edits.

This is *config only*: it records the order the user wants metadata providers run in, and which are
enabled. A future background worker consumes this; nothing here runs a provider. `load_config` is the
canonical reader: it always returns a valid, fully-populated, dependency-respecting list, so callers
(the worker, the template) never have to cope with missing/corrupt/partial saved state.

Ordering constraint is data-driven via each provider's `requires`: a provider must appear *after* the
one it requires (AcousticBrainz is keyed by MusicBrainz recording MBIDs, so it follows MusicBrainz).
"""
import json

_SETTING_KEY = "enrichment_order"

# Canonical registry: single source of truth. `key` names the settings entry holding that provider's
# API key (only Last.fm needs one today); `requires` names a provider that must come before it.
PROVIDERS = [
    {"name": "musicbrainz",    "label": "MusicBrainz",    "key": None,             "requires": None,
     "blurb": "Genre & release year, by recording match."},
    {"name": "acousticbrainz", "label": "AcousticBrainz", "key": None,             "requires": "musicbrainz",
     "blurb": "BPM, energy, danceability. Keyed by MusicBrainz ID, so it runs after it."},
    {"name": "lastfm",         "label": "Last.fm",        "key": "lastfm_api_key", "requires": None,
     "blurb": "Genre from dense crowd tags. Needs a free API key."},
    {"name": "discogs",        "label": "Discogs",        "key": None,             "requires": None,
     "blurb": "Genre & year. Works anonymously."},
    {"name": "deezer",         "label": "Deezer",         "key": None,             "requires": None,
     "blurb": "Genre & year from Deezer's catalog."},
]

_BY_NAME = {p["name"]: p for p in PROVIDERS}


def _repair_order(names):
    """Reorder `names` (already filtered to known providers) so every provider with a `requires`
    sits after its prerequisite. Stable otherwise. Repeats until stable to handle chains."""
    names = list(names)
    changed = True
    while changed:
        changed = False
        for i, name in enumerate(names):
            req = _BY_NAME[name]["requires"]
            if req is None or req not in names:
                continue
            if names.index(req) > i:                 # prerequisite is later. Move this one after it
                names.pop(i)
                names.insert(names.index(req) + 1, name)
                changed = True
                break
    return names


def load_config(store):
    """The canonical, always-valid provider config: an ordered list of dicts merged from the registry,
    each `{name, label, blurb, key, requires, enabled}`. Unknown saved names are dropped; providers
    absent from saved state are appended (enabled); ordering is repaired to respect `requires`."""
    try:
        saved = json.loads(store.get_setting(_SETTING_KEY) or "[]")
    except (ValueError, TypeError):
        saved = []
    enabled = {}
    order = []
    for item in saved if isinstance(saved, list) else []:
        name = item.get("name") if isinstance(item, dict) else None
        if name in _BY_NAME and name not in enabled:
            order.append(name)
            enabled[name] = bool(item.get("enabled", True))
    for name in _BY_NAME:                            # append providers not in saved state (new ones)
        if name not in enabled:
            order.append(name)
            enabled[name] = True
    order = _repair_order(order)
    return [{**_BY_NAME[name], "enabled": enabled[name]} for name in order]


def save_config(store, items):
    """Persist an ordered list of `{name, enabled}`. Raises ValueError if a name is unknown or the
    order violates a `requires` constraint (a provider before its prerequisite)."""
    cleaned = []
    seen = set()
    for item in items:
        name = item.get("name")
        if name not in _BY_NAME:
            raise ValueError(f"unknown provider: {name!r}")
        if name in seen:
            raise ValueError(f"duplicate provider: {name!r}")
        seen.add(name)
        cleaned.append({"name": name, "enabled": bool(item.get("enabled", True))})
    names = [c["name"] for c in cleaned]
    for i, name in enumerate(names):
        req = _BY_NAME[name]["requires"]
        if req is not None and req in names and names.index(req) > i:
            raise ValueError(f"{name!r} must come after {req!r}")
    store.set_setting(_SETTING_KEY, json.dumps(cleaned))
