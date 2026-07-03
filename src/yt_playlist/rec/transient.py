"""The transient model: a reactive, persistent read on recent interaction.

Inputs are signed track-key events at a point in recency: mood feedback (rec_mood), recent plays
(history), recent dislikes (rec_feedback kind='dislike'). Two derived views: facet leans
(genre/era/artist tokens) and an embedding centroid tilt.

Lifecycle (#85): every event decays on the wall clock with a per-source half-life (see the
*_halflife_d params); nothing is rank-decayed and there is no separate staleness relax. An event
fades the same way whether or not anything newer arrived.
"""
import numpy as np

from yt_playlist.util import genre_map
from yt_playlist.rec import embed, rec_params
from yt_playlist.rec.rec_dao import RecDao


def decay_weight(age_s, half_life_d) -> float:
    """#85 Wall-clock event decay: 0.5 ** (age_days / half_life_days). A fresh (or clock-skewed
    future) event weighs 1.0. This kernel is the whole lifecycle now: no rank decay, no separate
    staleness relax; an event fades on the clock whether or not anything newer arrived. #88's
    layered model instantiates this same kernel at several half-lives."""
    if age_s <= 0:
        return 1.0
    return 0.5 ** (age_s / (half_life_d * 86400.0))


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


def facet_leans(store, now) -> dict:
    """{facet: signed strength}: the token view of the transient model. #85: every source decays on
    the wall clock (per-source half-life params); a month-old vibe tap is nearly gone even if the
    user never interacted again."""
    gp = rec_params.get_param
    play_w, like_w, dislike_w = (gp(store, "play_transient_w"), gp(store, "like_transient_w"),
                                 gp(store, "dislike_transient_w"))
    play_hl, mood_hl = gp(store, "play_halflife_d"), gp(store, "mood_halflife_d")
    like_hl, dislike_hl = gp(store, "like_halflife_d"), gp(store, "dislike_halflife_d")
    limit = gp(store, "recent_play_limit")
    leans: dict = {}

    def add(keys, signed, drop_artist=False):
        if not keys or signed == 0:
            return
        fac = facets_for(store, keys)
        n = len(set(keys)) or 1
        for f, fk in fac.items():
            if drop_artist and f.startswith("artist:"):
                continue
            leans[f] = leans.get(f, 0.0) + signed * (len(fk) / n)

    for ts, direction, keys in store.recent_mood_events():
        add(keys, direction * decay_weight(now - ts, mood_hl))
    for k, ts in store.recent_plays_with_ts(limit=limit):
        add([k], play_w * decay_weight(now - ts, play_hl))
    for k, ts in store.recent_liked_with_ts(limit=limit):
        add([k], like_w * decay_weight(now - ts, like_hl))
    # #54: a dislike is a verdict on THAT track, not the artist (the track itself is hard-suppressed
    # via suppressed_keys). Genre/era leans stay, but #85 they now FADE by age instead of pressing at
    # full strength forever.
    for k, ts in store.disliked_with_ts():
        add([k], -dislike_w * decay_weight(now - ts, dislike_hl), drop_artist=True)
    return leans


def play_facet_leans(store, now) -> dict:
    """The recent-plays slice of `facet_leans`, in isolation.

    `facet_leans` blends every transient source (mood, plays, likes, dislikes). The play-exposure
    graduation funnel (graduation.graduate_play_exposure) must graduate plays SPECIFICALLY, because
    likes / dislikes / vibe taps already graduate at event time and would otherwise double-count. So
    this returns only the positive, wall-clock-decayed push from recent plays (#85: per-event age, no
    rank, no separate staleness relax). When listening stops the recent window drains and each play
    ages out on its own, so this falls to {} over time, which is the funnel's natural off-switch.
    """
    gp = rec_params.get_param
    play_w = gp(store, "play_transient_w")
    play_hl = gp(store, "play_halflife_d")
    limit = gp(store, "recent_play_limit")
    leans: dict = {}
    for k, ts in store.recent_plays_with_ts(limit=limit):
        for f in facets_for(store, [k]):                # one key -> its facets, each gets the play push
            leans[f] = leans.get(f, 0.0) + play_w * decay_weight(now - ts, play_hl)
    return leans


def facet_multiplier(lean, gain, lo, hi) -> float:
    """Map a signed facet lean to a positive ranking multiplier; 1.0 = neutral, clamped to [lo, hi]."""
    return max(lo, min(hi, 1.0 + gain * lean))


def centroid_tilt(store, now, V, idx):
    """Unit embedding direction from the unified transient stream: mood events, recent plays, and
    recent likes, each wall-clock decayed by its own half-life (#85). None if quiet. Decay is
    per-event and internal; callers apply no external freshness factor."""
    gp = rec_params.get_param
    limit = gp(store, "recent_play_limit")
    events = store.recent_mood_events()
    plays = store.recent_plays_with_ts(limit=limit)
    likes = store.recent_liked_with_ts(limit=limit)
    if not events and not plays and not likes:
        return None
    mood_hl = gp(store, "mood_halflife_d")
    play_hl, play_w = gp(store, "play_halflife_d"), gp(store, "play_transient_w")
    like_hl, like_w = gp(store, "like_halflife_d"), gp(store, "like_transient_w")
    # tilt accumulates a wall-clock-decayed vector sum of UNIT directions across all transient
    # sources; each contribution is decayed by decay_weight(age, half_life) (fresher events count for
    # more) and the whole sum is renormalized to a single unit direction at the end. Each mood event
    # adds its signed centroid direction (direction = +1 like / -1 dislike).
    tilt = np.zeros(V.shape[1], dtype=np.float64)
    for ts, direction, keys in events:
        rows = [idx[k] for k in keys if k in idx]
        if not rows:
            continue
        c = V[rows].mean(0)
        nrm = np.linalg.norm(c)
        if nrm == 0:
            continue
        tilt += direction * decay_weight(now - ts, mood_hl) * (c / nrm)

    def _add_dir(pairs, w_base, half_life_d):
        # Add each track's own unit vector, scaled by its source weight (w_base) and wall-clock decay.
        for k, ts in pairs:
            if k not in idx:
                continue
            v = V[idx[k]]
            nrm = np.linalg.norm(v)
            if nrm == 0:
                continue
            nonlocal tilt
            tilt = tilt + w_base * decay_weight(now - ts, half_life_d) * (v / nrm)

    _add_dir(plays, play_w, play_hl)
    _add_dir(likes, like_w, like_hl)
    n = np.linalg.norm(tilt)
    return tilt / n if n > 0 else None                   # unit direction, or None when nothing accumulated


def audio_centroid_tilt(store, now):
    """Unit direction in the audio-aware CONTENT vector space toward recent listening, so ranking can
    lean to the SOUND of what you have been playing (tempo / energy / mood), not just its genre/era/
    artist facets or its co-occurrence neighbours. The content-space sibling of `centroid_tilt`: same
    transient stream (mood events, recent plays, recent likes), same recency weighting, but built from
    the persisted content vectors (embed.load_content_vectors), which carry the z-scored audio block.

    Returns None when the content model is unbuilt or no recent track has a content vector, so a cold
    or quiet user is exactly as today. Audio is sparse and growing: tracks without a content vector
    simply do not contribute, so the tilt is defined by whatever covered tracks the user has played
    (graceful degradation). Decay is per-event and internal (#85); callers apply no external freshness
    factor, mirroring `centroid_tilt`.

    This is the producer only. The scorer applies it (a content-space cosine term parallel to the
    collaborative `_apply_mood`); see the wiring note that ships with this change.
    """
    _ckeys, CV, cidx = embed.load_content_vectors(store)
    if CV is None:
        return None
    gp = rec_params.get_param
    mood_hl = gp(store, "mood_halflife_d")
    play_hl, play_w = gp(store, "play_halflife_d"), gp(store, "play_transient_w")
    like_hl, like_w = gp(store, "like_halflife_d"), gp(store, "like_transient_w")
    limit = gp(store, "recent_play_limit")
    tilt = np.zeros(CV.shape[1], dtype=np.float64)

    for ts, direction, keys in store.recent_mood_events():     # newest-first
        rows = [cidx[k] for k in keys if k in cidx]
        if not rows:
            continue
        c = CV[rows].mean(0)
        nrm = np.linalg.norm(c)
        if nrm == 0:
            continue
        tilt += direction * decay_weight(now - ts, mood_hl) * (c / nrm)

    def _add_dir(pairs, w_base, half_life_d):
        nonlocal tilt
        for k, ts in pairs:
            if k not in cidx:
                continue
            tilt = tilt + w_base * decay_weight(now - ts, half_life_d) * CV[cidx[k]]  # rows already unit

    _add_dir(store.recent_plays_with_ts(limit=limit), play_w, play_hl)
    _add_dir(store.recent_liked_with_ts(limit=limit), like_w, like_hl)
    n = np.linalg.norm(tilt)
    return tilt / n if n > 0 else None
