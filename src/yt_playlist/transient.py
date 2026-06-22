"""The transient model: a reactive, persistent read on recent interaction.

Inputs are signed track-key events at a point in recency — mood feedback (rec_mood), recent plays
(history), recent dislikes (rec_feedback kind='dislike'). Two derived views: facet leans
(genre/era/artist tokens) and an embedding centroid tilt. Lifecycle: persistent (no wall-clock
expiry), reactive by interaction rank, relaxes only as sync goes stale. See the design spec.
"""
from yt_playlist import genre_map
from yt_playlist.rec_dao import RecDao


def facets_for(store, keys) -> dict:
    """Map track identity_keys to the rec axes they carry:
    {'genre:<fam>': [keys...], 'era:<decade>': [keys...], 'artist:<name>': [keys...]}.
    A key contributes to at most one axis of each type (its own family / decade / artist)."""
    keys = list(dict.fromkeys(keys))
    if not keys:
        return {}
    dao = RecDao(store)
    genres, decades, artists = dao.track_genres(keys), dao.track_decades(keys), dao.track_artists(keys)
    out: dict = {}
    for k in keys:
        if k in genres:
            fam = genre_map.family(genres[k])
            if fam:
                out.setdefault(f"genre:{fam}", []).append(k)
        if k in decades:
            out.setdefault(f"era:{decades[k]}", []).append(k)
        if k in artists:
            out.setdefault(f"artist:{artists[k]}", []).append(k)
    return out
