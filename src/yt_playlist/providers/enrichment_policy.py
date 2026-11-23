"""Detached provider order, configuration and coverage fingerprints."""
import hashlib
import json

from yt_playlist.util import genre_catalog

SETTING_KEY = 'enrichment_order'
GENRE_POLICY_VERSION = 3  # Structured performing-artist evidence alongside scalar metadata.
_GENRE_PROVIDERS = frozenset(('musicbrainz', 'lastfm', 'discogs', 'deezer'))

PROVIDERS = [
    {"name": "musicbrainz", "label": "MusicBrainz", "key": None, "requires": None,
     "blurb": "Genre & release year, by recording match."},
    {"name": "acousticbrainz", "label": "AcousticBrainz", "key": None, "requires": "musicbrainz",
     "blurb": "Tempo and native sound classifiers. Looks up the MusicBrainz recording ID."},
    {"name": "lastfm", "label": "Last.fm", "key": "lastfm_api_key", "requires": None,
     "blurb": "Better genre labels than the free, public sources used by default. Register for a free API key to improve recommendations over time."},
    {"name": "discogs", "label": "Discogs", "key": None, "requires": None,
     "blurb": "Genre & year search needs DISCOGS_TOKEN or discogs_token in config.toml. Linked release facts work without a token."},
    {"name": "deezer", "label": "Deezer", "key": None, "requires": None,
     "blurb": "Genre & year from Deezer's catalog."},
]
_BY_NAME = {p['name']: p for p in PROVIDERS}


def configuration_fingerprints(config, facts, vocabulary, *, policy_version=GENRE_POLICY_VERSION):
    result = {}
    for p in config:
        fact = facts.get(p['name'])
        if fact is None:
            continue
        status = 'disabled' if not p['enabled'] else 'available' if fact['available'] else 'unavailable'
        inputs = {'version': 1, 'key': fact['credential']}
        if p['name'] in _GENRE_PROVIDERS:
            inputs['genres'] = {'catalog': genre_catalog.REVISION, 'policy': policy_version,
                                'enabled': vocabulary}
        fingerprint = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
        result[p['name']] = {'status': status, 'fingerprint': fingerprint}
    return result


def repair_order(names):
    names = list(names)
    changed = True
    while changed:
        changed = False
        for i, name in enumerate(names):
            req = _BY_NAME[name]['requires']
            if req is not None and req in names and names.index(req) > i:
                names.pop(i)
                names.insert(names.index(req) + 1, name)
                changed = True
                break
    return names


def normalize_config(raw):
    try:
        saved = json.loads(raw or '[]')
    except (ValueError, TypeError):
        saved = []
    enabled, order = {}, []
    for item in saved if isinstance(saved, list) else []:
        name = item.get('name') if isinstance(item, dict) else None
        if name in _BY_NAME and name not in enabled:
            order.append(name)
            enabled[name] = bool(item.get('enabled', True))
    for name in _BY_NAME:
        if name not in enabled:
            order.append(name)
            enabled[name] = True
    return [{**_BY_NAME[name], 'enabled': enabled[name]} for name in repair_order(order)]


def serialize_config(items):
    cleaned, seen = [], set()
    for item in items:
        name = item.get('name')
        if name not in _BY_NAME:
            raise ValueError(f'unknown provider: {name!r}')
        if name in seen:
            raise ValueError(f'duplicate provider: {name!r}')
        seen.add(name)
        cleaned.append({'name': name, 'enabled': bool(item.get('enabled', True))})
    names = [c['name'] for c in cleaned]
    for i, name in enumerate(names):
        req = _BY_NAME[name]['requires']
        if req is not None and req in names and names.index(req) > i:
            raise ValueError(f'{name!r} must come after {req!r}')
    return json.dumps(cleaned)
