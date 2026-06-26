"""The transient model: a reactive, persistent read on recent interaction.

Inputs are signed track-key events at a point in recency: mood feedback (rec_mood), recent plays
(history), recent dislikes (rec_feedback kind='dislike'). Two derived views: facet leans
(genre/era/artist tokens) and an embedding centroid tilt. Lifecycle: persistent (no wall-clock
expiry), reactive by interaction rank, relaxes only as sync goes stale. See the design spec.
"""
import numpy as np

from yt_playlist.util import genre_map
from yt_playlist.rec import embed, rec_params
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
            sub = genre_map.subgenre(genres[k])
            if sub and sub != fam:
                out.setdefault(f"genre:{sub}", []).append(k)
        if k in decades:
            out.setdefault(f"era:{decades[k]}", []).append(k)
        if k in artists:
            out.setdefault(f"artist:{artists[k]}", []).append(k)
    return out


def staleness_factor(store, now) -> float:
    """1.0 while sync is fresh; decays past SYNC_STALE_S with a STALE_DECAY_HALFLIFE_D half-life.

    Uses the most recent sync of EITHER kind - a full sync (`last_sync_at`) or a quick plays/auto
    sync (`last_plays_sync_at`) - mirroring recommend.sync_status. A recent auto-sync brings fresh
    play data, so it must keep the transient model live (otherwise the model decays even though plays
    are current)."""
    stamps = [float(s) for s in (store.get_setting("last_sync_at"),
                                 store.get_setting("last_plays_sync_at")) if s is not None]
    if not stamps:
        return 1.0
    age = now - max(stamps)
    if age <= rec_params.SYNC_STALE_S:
        return 1.0
    halflife_d = rec_params.get_param(store, "stale_decay_halflife_d")
    return 0.5 ** ((age - rec_params.SYNC_STALE_S) / (halflife_d * 86400.0))


def facet_leans(store, now) -> dict:
    """{facet: signed strength}: the token view of the transient model, from mood feedback (rank-
    weighted), recent plays (positive), and recent dislikes (negative), scaled by staleness."""
    gp = rec_params.get_param
    a = gp(store, "mood_recency_alpha")
    play_w = gp(store, "play_transient_w")
    like_w = gp(store, "like_transient_w")
    dislike_w = gp(store, "dislike_transient_w")
    limit = gp(store, "recent_play_limit")
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
    for rank, k in enumerate(store.recent_keys_ordered(0, limit=limit)):
        add([k], play_w * ((1.0 - a) ** rank))
    for rank, k in enumerate(store.recent_liked_keys(limit=limit)):
        add([k], like_w * ((1.0 - a) ** rank))
    for k in store.disliked_identity_keys():
        add([k], -dislike_w)
    s = staleness_factor(store, now)
    return {f: v * s for f, v in leans.items()}


def play_facet_leans(store, now) -> dict:
    """The recent-plays slice of `facet_leans`, in isolation, staleness-scaled.

    `facet_leans` blends every transient source (mood, plays, likes, dislikes). The play-exposure
    graduation funnel (graduation.graduate_play_exposure) must graduate plays SPECIFICALLY, because
    likes / dislikes / vibe taps already graduate at event time and would otherwise double-count. So
    this returns only the positive, recency-weighted, staleness-scaled push from recent plays. When
    listening stops the recent window drains (and staleness damps it), so this falls to {} on its own,
    which is the funnel's natural off-switch.
    """
    gp = rec_params.get_param
    a = gp(store, "mood_recency_alpha")
    play_w = gp(store, "play_transient_w")
    limit = gp(store, "recent_play_limit")
    leans: dict = {}
    for rank, k in enumerate(store.recent_keys_ordered(0, limit=limit)):
        for f in facets_for(store, [k]):                # one key -> its facets, each gets the play push
            leans[f] = leans.get(f, 0.0) + play_w * ((1.0 - a) ** rank)
    s = staleness_factor(store, now)
    return {f: v * s for f, v in leans.items()}


def facet_multiplier(lean, gain, lo, hi) -> float:
    """Map a signed facet lean to a positive ranking multiplier; 1.0 = neutral, clamped to [lo, hi]."""
    return max(lo, min(hi, 1.0 + gain * lean))


def centroid_tilt(store, now, V, idx):
    """Unit embedding direction from the unified transient stream: mood events, recent plays, and
    recent likes, all interaction-rank weighted. None if quiet. Staleness is applied by the caller."""
    gp = rec_params.get_param
    limit = gp(store, "recent_play_limit")
    events = store.recent_mood_events()
    plays = store.recent_keys_ordered(0, limit=limit)
    likes = store.recent_liked_keys(limit=limit)
    if not events and not plays and not likes:
        return None
    a = gp(store, "mood_recency_alpha")
    play_w = gp(store, "play_transient_w")
    like_w = gp(store, "like_transient_w")
    tilt = np.zeros(V.shape[1], dtype=np.float64)
    for rank, (_ts, direction, keys) in enumerate(events):
        rows = [idx[k] for k in keys if k in idx]
        if not rows:
            continue
        c = V[rows].mean(0)
        nrm = np.linalg.norm(c)
        if nrm == 0:
            continue
        tilt += direction * ((1.0 - a) ** rank) * (c / nrm)

    def _add_dir(keys, w_base):
        for rank, k in enumerate(keys):
            if k not in idx:
                continue
            v = V[idx[k]]
            nrm = np.linalg.norm(v)
            if nrm == 0:
                continue
            nonlocal tilt
            tilt = tilt + w_base * ((1.0 - a) ** rank) * (v / nrm)

    _add_dir(plays, play_w)
    _add_dir(likes, like_w)
    n = np.linalg.norm(tilt)
    return tilt / n if n > 0 else None


def audio_centroid_tilt(store, now):
    """Unit direction in the audio-aware CONTENT vector space toward recent listening, so ranking can
    lean to the SOUND of what you have been playing (tempo / energy / mood), not just its genre/era/
    artist facets or its co-occurrence neighbours. The content-space sibling of `centroid_tilt`: same
    transient stream (mood events, recent plays, recent likes), same recency weighting, but built from
    the persisted content vectors (embed.load_content_vectors), which carry the z-scored audio block.

    Returns None when the content model is unbuilt or no recent track has a content vector, so a cold
    or quiet user is exactly as today. Audio is sparse and growing: tracks without a content vector
    simply do not contribute, so the tilt is defined by whatever covered tracks the user has played
    (graceful degradation). Staleness is applied by the caller, mirroring `centroid_tilt`.

    This is the producer only. The scorer applies it (a content-space cosine term parallel to the
    collaborative `_apply_mood`); see the wiring note that ships with this change.
    """
    _ckeys, CV, cidx = embed.load_content_vectors(store)
    if CV is None:
        return None
    gp = rec_params.get_param
    a = gp(store, "mood_recency_alpha")
    play_w = gp(store, "play_transient_w")
    like_w = gp(store, "like_transient_w")
    limit = gp(store, "recent_play_limit")
    tilt = np.zeros(CV.shape[1], dtype=np.float64)

    for rank, (_ts, direction, keys) in enumerate(store.recent_mood_events()):     # newest-first
        rows = [cidx[k] for k in keys if k in cidx]
        if not rows:
            continue
        c = CV[rows].mean(0)
        nrm = np.linalg.norm(c)
        if nrm == 0:
            continue
        tilt += direction * ((1.0 - a) ** rank) * (c / nrm)

    def _add_dir(keys, w_base):
        nonlocal tilt
        for rank, k in enumerate(keys):
            if k not in cidx:
                continue
            tilt = tilt + w_base * ((1.0 - a) ** rank) * CV[cidx[k]]    # CV rows are already unit

    _add_dir(store.recent_keys_ordered(0, limit=limit), play_w)
    _add_dir(store.recent_liked_keys(limit=limit), like_w)
    n = np.linalg.norm(tilt)
    return tilt / n if n > 0 else None
