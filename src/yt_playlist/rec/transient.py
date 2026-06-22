"""The transient model: a reactive, persistent read on recent interaction.

Inputs are signed track-key events at a point in recency — mood feedback (rec_mood), recent plays
(history), recent dislikes (rec_feedback kind='dislike'). Two derived views: facet leans
(genre/era/artist tokens) and an embedding centroid tilt. Lifecycle: persistent (no wall-clock
expiry), reactive by interaction rank, relaxes only as sync goes stale. See the design spec.
"""
import numpy as np

from yt_playlist.rec import genre_map, rec_params
from yt_playlist.rec.rec_dao import RecDao


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


def staleness_factor(store, now) -> float:
    """1.0 while sync is fresh; decays past SYNC_STALE_S with a STALE_DECAY_HALFLIFE_D half-life."""
    last = store.get_setting("last_sync_at")
    if last is None:
        return 1.0
    age = now - float(last)
    if age <= rec_params.SYNC_STALE_S:
        return 1.0
    return 0.5 ** ((age - rec_params.SYNC_STALE_S) / (rec_params.STALE_DECAY_HALFLIFE_D * 86400.0))


def facet_leans(store, now) -> dict:
    """{facet: signed strength} — the token view of the transient model, from mood feedback (rank-
    weighted), recent plays (positive), and recent dislikes (negative), scaled by staleness."""
    a = rec_params.MOOD_RECENCY_ALPHA
    leans: dict = {}

    def add(keys, signed):
        if not keys or signed == 0:
            return
        fac = facets_for(store, keys)
        n = len(set(keys)) or 1
        for f, fk in fac.items():
            leans[f] = leans.get(f, 0.0) + signed * (len(fk) / n)

    for rank, (_ts, direction, keys) in enumerate(store.recent_mood_events()):     # newest-first
        add(keys, direction * ((1.0 - a) ** rank))
    for rank, k in enumerate(store.recent_keys_ordered(0, limit=rec_params.RECENT_PLAY_LIMIT)):
        add([k], rec_params.PLAY_TRANSIENT_W * ((1.0 - a) ** rank))
    for k in store.disliked_identity_keys():
        add([k], -rec_params.DISLIKE_TRANSIENT_W)
    s = staleness_factor(store, now)
    return {f: v * s for f, v in leans.items()}


def facet_multiplier(lean) -> float:
    """Map a signed facet lean to a positive ranking multiplier; 1.0 = neutral, clamped."""
    return max(rec_params.FACET_MULT_MIN,
               min(rec_params.FACET_MULT_MAX, 1.0 + rec_params.FACET_GAIN * lean))


def centroid_tilt(store, now, V, idx):
    """Unit embedding direction from mood events, interaction-rank weighted (persistent successor to
    the old mood_tilt). None if quiet. Staleness is applied by the caller (it scales the blend)."""
    events = store.recent_mood_events()
    if not events:
        return None
    a = rec_params.MOOD_RECENCY_ALPHA
    tilt = np.zeros(V.shape[1], dtype=np.float64)
    for rank, (_ts, direction, keys) in enumerate(events):
        rows = [idx[k] for k in keys if k in idx]
        if not rows:
            continue
        c = V[rows].mean(0)
        n = np.linalg.norm(c)
        if n == 0:
            continue
        tilt += direction * ((1.0 - a) ** rank) * (c / n)
    n = np.linalg.norm(tilt)
    return tilt / n if n > 0 else None
