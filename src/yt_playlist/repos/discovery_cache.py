"""Prepare complete cache pruning before reserving the discovery cleanup writer."""
from yt_playlist.core import json_cache
from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.repos.rec_proposals import encode_proposals
from yt_playlist.repos import proposal_cache


def prepare(rows, gone_keys):
    """Prune decoded values in place; never build a second complete cache tree."""
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


def stage(repo, prepared):
    """Keep large pruned pools outside the membership deletion transaction."""
    return [(proposal_cache.stage(repo, surface, encoded) if surface in proposal_cache.SURFACES else encoded,
             surface) for encoded, surface in prepared]


def publish(conn, prepared):
    """Publish prepared values; the caller owns the guard and transaction."""
    for value, surface in prepared:
        if isinstance(value, proposal_cache.PreparedCache):
            proposal_cache.finish(conn, value)
        else:
            conn.execute('UPDATE rec_proposals SET payload=? WHERE surface=?', (value, surface))
