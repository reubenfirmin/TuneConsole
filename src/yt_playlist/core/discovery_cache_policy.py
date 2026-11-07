"""Remove expired candidates from detached recommendation documents."""
from yt_playlist.core import json_cache
from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.proposal_codec import encode_proposals


def prepare(rows, gone_keys):
    """Prune decoded values in place without copying a complete cache tree."""
    visited = 0

    def prune(value):
        nonlocal visited
        visited += 1
        if visited % 256 == 0:
            check_cancellable_work()
        changed = False
        if isinstance(value, list):
            retained = 0
            for item in value:
                if isinstance(item, dict) and item.get('key', item.get('identity_key')) in gone_keys:
                    changed = True
                    continue
                changed = prune(item) or changed
                value[retained] = item
                retained += 1
            del value[retained:]
        elif isinstance(value, dict):
            for item in value.values():
                changed = prune(item) or changed
        return changed

    prepared = []
    for row in rows:
        check_cancellable_work()
        payload = json_cache.decode(row['payload'])
        if prune(payload):
            prepared.append((encode_proposals(row['surface'], payload), row['surface']))
        del payload
    check_cancellable_work()
    return prepared
