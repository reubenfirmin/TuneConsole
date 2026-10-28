"""Versioned compression for disposable JSON caches; plain JSON remains readable."""
import base64
import binascii
from copy import deepcopy
import json
import threading
import zlib

from yt_playlist.core import bounded_json
from yt_playlist.core.activity import check_cancellable_work

MARKER = '__yt_playlist_cache__'
LEGACY_FORMAT = 'zlib-json-v1'
FORMAT = 'zlib-json-v2'
MIN_BYTES = 64 * 1024


def _pack(encoded):
    return base64.b64encode(zlib.compress(encoded.encode(), level=1)).decode('ascii')


def unpack(encoded):
    try:
        return zlib.decompress(base64.b64decode(encoded, validate=True)).decode()
    except (ValueError, TypeError, binascii.Error, zlib.error, UnicodeError) as exc:
        raise ValueError('Invalid compressed recommendation cache') from exc


def _share_profiles(value, profiles, positions):
    """Store repeated musical evidence once, without changing any candidate fields."""
    if isinstance(value, list):
        return [_share_profiles(item, profiles, positions) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key == 'musical' and isinstance(item, dict):
            encoded = json.dumps(item, separators=(',', ':'))
            if encoded not in positions:
                positions[encoded] = len(profiles)
                profiles.append(encoded)
            # Every dictionary in this field is encoded, including dictionaries
            # that happen to resemble a reference in the original data.
            result[key] = {'profile': positions[encoded]}
        else:
            result[key] = _share_profiles(item, profiles, positions)
    return result


def _restore_profiles(value, profiles):
    if isinstance(value, list):
        return [_restore_profiles(item, profiles) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key == 'musical' and isinstance(item, dict):
            position = item.get('profile')
            if (set(item) != {'profile'} or type(position) is not int
                    or not 0 <= position < len(profiles)):
                raise ValueError('Invalid compressed recommendation cache profile')
            # Callers prune and annotate candidates independently. Shared storage
            # must not introduce shared mutable dictionaries after decoding.
            profile = json.loads(profiles[position])
            if not isinstance(profile, dict):
                raise ValueError('Invalid compressed recommendation cache profile')
            result[key] = profile
        else:
            result[key] = _restore_profiles(item, profiles)
    return result


def _profiles(encoded):
    profiles = bounded_json.loads(unpack(encoded))
    if not isinstance(profiles, list) or any(not isinstance(item, str) for item in profiles):
        raise ValueError('Invalid compressed recommendation cache profiles')
    return profiles


def encode(value, *, group_pools=False, references=()):
    encoded = json.dumps(value, separators=(',', ':'))
    if len(encoded) < MIN_BYTES:
        return encoded
    profiles, positions = [], {}
    body = _share_profiles(value, profiles, positions)
    envelope = {MARKER: FORMAT}
    if group_pools and isinstance(value, dict):
        # The full reader joins these two sections. Keeping them in both blobs
        # previously wrote the entire global candidate collection twice.
        pools = {key: body.pop(key) for key in ('all', '_meta') if key in body}
        envelope['groups'] = _pack(json.dumps(pools, separators=(',', ':')))
    envelope['json'] = _pack(json.dumps(body, separators=(',', ':')))
    envelope['profiles'] = _pack(json.dumps(profiles, separators=(',', ':')))
    # Retention can inspect these without inflating a large body under its
    # write transaction. Keep the existing pool_policy reference convention.
    envelope['policies'] = [{'pool_policy': version} for version in sorted(set(references))]
    compact = json.dumps(envelope, separators=(',', ':'))
    return compact if len(compact.encode()) < len(encoded.encode()) else encoded


def _select(value, keys):
    return {key: item for key, item in value.items() if key in keys} if (
        keys is not None and isinstance(value, dict)) else value


def _read(encoded, keys):
    """Keep compressed-profile references until the caller selects its rows."""
    envelope_keys = {MARKER, 'json', 'groups', 'profiles'}
    value = bounded_json.loads(encoded, keys=keys | envelope_keys if keys is not None else None)
    if isinstance(value, dict) and value.get(MARKER) == LEGACY_FORMAT:
        return bounded_json.loads(unpack(value.get('json')), keys=keys), None
    if isinstance(value, dict) and value.get(MARKER) == FORMAT:
        body = bounded_json.loads(unpack(value.get('json')), keys=keys)
        if 'groups' in value and (keys is None or keys & {'all', '_meta'}):
            groups = bounded_json.loads(unpack(value['groups']), keys=keys)
            if not isinstance(body, dict) or not isinstance(groups, dict):
                raise ValueError('Invalid compressed recommendation cache groups')
            body.update(groups)
        return body, _profiles(value.get('profiles'))
    return value, None


def _finish(value, profiles, keys, project, *, detach=False):
    value = _select(value, keys)
    if project is not None:
        value = project(value)
    if profiles is not None:
        return _restore_profiles(value, profiles)
    return deepcopy(value) if detach else value


def decode(encoded, *, keys=None, project=None):
    """Reconstruct complete values, or selected top-level fields before restoring profiles.

    A consumer of Radio pools does not need to allocate every Home preset's
    musical evidence. Selection changes only this detached read, never storage.
    An optional projection may select rows using ordinary metadata; musical
    dictionaries still contain opaque references until the projection returns.
    """
    keys = frozenset(keys) if keys is not None else None
    value, profiles = _read(encoded, keys)
    return _finish(value, profiles, keys, project)


class DecodeCache:
    """Share one exact payload's parsing, with independently mutable results.

    Projections must only read their input. They run outside the parsing lock;
    the selected result alone is copied/restored, rather than all stored pools.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._key = None
        self._value = None

    def decode(self, encoded, *, keys=None, project=None):
        keys = frozenset(keys) if keys is not None else None
        key = encoded, keys
        # Do not hold a serving/database lock while waiting for another decoder.
        # A stopped constructor must remain cancellable even on a shared miss.
        while not self._lock.acquire(timeout=.05):
            check_cancellable_work()
        try:
            check_cancellable_work()
            if self._key != key:
                value = _read(encoded, keys)
                self._key, self._value = key, value
            value, profiles = self._value
        finally:
            self._lock.release()
        return _finish(value, profiles, keys, project, detach=True)


def decode_groups(encoded, cache_format, profiles=None):
    if cache_format == LEGACY_FORMAT:
        return unpack(encoded)
    if cache_format == FORMAT:
        groups = _restore_profiles(bounded_json.loads(unpack(encoded)), _profiles(profiles))
        if not isinstance(groups, dict):
            raise ValueError('Invalid compressed recommendation cache groups')
        return json.dumps({'all': groups.get('all', {}), '_meta': groups.get('_meta', {})},
                          separators=(',', ':'))
    return encoded
