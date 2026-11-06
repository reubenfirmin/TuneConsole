"""Bounded catalog hydration for continuing Radio style streams.

Membership is prepared by the existing recommendation worker. Legacy sessions
without that index retain the #286 genre fallback until their next launch.
"""
from itertools import islice

from yt_playlist.rec.radio_genres import GenrePolicy


def fallback_pool(store, allowed, *, limit):
    """Fill a bounded cold pool from eligible library rows, past rejected pages."""
    from yt_playlist.core.activity import check_cancellable_work

    tracks, seen, after = [], set(), 0
    while len(tracks) < limit:
        check_cancellable_work()
        rows = store.radio_catalog.fallback_tracks(limit, after=after)
        check_cancellable_work()
        if not rows:
            break
        after = rows[-1]['id']
        for track in rows:
            if track['key'] in seen or not allowed(track):
                continue
            seen.add(track['key'])
            tracks.append(track)
            if len(tracks) == limit:
                return tracks
        if len(rows) < limit:
            break
    return tracks


def cluster_pool(store, keys, excluded, excluded_keys, allowed, *, limit):
    """Hydrate eligible recordings, scanning past stale/removed uploads in pages.

    Each SQL read and admitted vector batch is bounded by limit. No ranking,
    fitting, or full-library vector read occurs on this path.
    """
    tracks, seen = [], set(excluded_keys)
    remaining = (key for key in keys if key not in seen)
    while batch := list(islice(remaining, limit)):
        from yt_playlist.core.activity import check_cancellable_work
        check_cancellable_work()
        rows = store.radio_catalog.cluster_tracks(batch)
        for row in rows:
            track = dict(row)
            if track['key'] in seen or track['video_id'] in excluded or not allowed(track):
                continue
            seen.add(track['key'])
            tracks.append(track)
            if len(tracks) == limit:
                return tracks
    return tracks


def themed_pool(store, genres: GenrePolicy, excluded, excluded_keys, allowed, *, limit):
    """Read another eligible genre batch, including aliases and upload overrides."""
    if not genres.pinned or not (genres.current or {}).get("genre"):
        return []
    labels = [label for label in store.radio_catalog.genre_labels() if genres.allows({'genre': label})]
    if not labels:
        return []
    tracks, seen, after = [], set(excluded_keys), 0
    while len(tracks) < limit:
        rows = store.radio_catalog.genre_tracks(labels, after, limit)
        if not rows:
            break
        after = rows[-1]["id"]
        for row in rows:
            track = dict(row)
            if track["video_id"] in excluded or track["key"] in seen or not allowed(track):
                continue
            seen.add(track["key"])
            tracks.append(track)
            if len(tracks) == limit:
                break
    return tracks
