"""The transient model: a reactive, persistent read on recent interaction.

Inputs are signed track-key events at a point in recency: mood feedback (mood_events), recent plays
(history), recent ACTION-provenance likes (rec_feedback kind='like' observed live through the
extension; sync-discovered likes carry no true action timestamp and are excluded at the repo level,
see recent_liked_with_ts), and recent skips (#84,
player_events track_exit/bye rows classified at read time). Two derived views: facet leans
(genre/era/artist tokens) and an embedding centroid tilt.

Lifecycle (#85): every event decays on the wall clock with a per-source half-life (see the
*_halflife_d params); nothing is rank-decayed and there is no separate staleness relax. An event
fades the same way whether or not anything newer arrived.
"""
import json
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

from yt_playlist.rec import embedding_vectors as vectors, rec_params
from yt_playlist.rec.transient_math import decay_weight, facet_multiplier
from yt_playlist.core.listening_calendar import day_at

# Historical generated radio playlist IDs remain provenance after the deck controller's removal.
_RADIO_SETTING_KEYS = ("radio_playlist_ytm", "radio_playlist_a_ytm", "radio_playlist_b_ytm")
PREPARED_LEANS_INTERVAL_S = 300


def recent_cutoff(store, now):
    # Coarse listening days enter through history at day precision. A source's
    # half-life controls its strength inside this bounded horizon.
    return now - rec_params.get_param(store, 'recent_horizon_days') * 86400


@dataclass(frozen=True)
class DatedPlay:
    day: str
    timezone: str
    source: str
    confidence: float = .5

    def age(self, now):
        return (date.fromisoformat(day_at(now, self.timezone)) - date.fromisoformat(self.day)).days * 86400


def source_age(now, observed):
    return observed.age(now) if isinstance(observed, DatedPlay) else now - observed


def source_confidence(observed):
    return observed.confidence if isinstance(observed, DatedPlay) else 1.


def recent_play_evidence(store, now, *, limit, cutoff):
    excluded = radio_list_ids(store)
    quarantined = store.rec_eligibility.generated_only_keys()
    plays = {}
    for row in store.history.play_events_since(cutoff, excluded, taste_only=True):
        if row['played_at'] <= now and row['identity_key'] not in quarantined:
            plays[row['identity_key']] = row['played_at']
    # Coarse observations have a local day, without a defensible time of day.
    first = date.fromisoformat(day_at(cutoff, None)) - timedelta(days=1)
    last = date.fromisoformat(day_at(now, None)) + timedelta(days=1)
    for row in store.history.recent_calendar_evidence(first.isoformat(), last.isoformat(), now, excluded):
        key = row['identity_key']
        if key in quarantined:
            continue
        observed = DatedPlay(row['day'], row['timezone'], row['source'])
        age = observed.age(now)
        if 0 <= age <= now - cutoff and (key not in plays or age < source_age(now, plays[key])):
            plays[key] = observed
    return sorted(plays.items(), key=lambda item: (source_age(now, item[1]), item[0]))[:limit]


def recent_sources(store, now):
    limit = rec_params.get_param(store, 'recent_play_limit')
    cutoff = recent_cutoff(store, now)
    events = [(at, direction, keys) for at, direction, keys in store.rec_signals.recent_mood_events()
              if cutoff <= at <= now]
    plays = store.memoized_read(('recent-listener-plays', now, cutoff, limit),
                                lambda: recent_play_evidence(store, now, limit=limit, cutoff=cutoff))
    likes = [(key, at) for key, at in store.rec_model.recent_liked_with_ts(limit=limit) if cutoff <= at <= now]
    skips = [(key, at) for key, at in store.player_events.recent_skips_with_ts(cutoff) if at <= now]
    return events, plays, likes, skips


def recent_feedback(store, now):
    def collect():
        rows = store.rec_model.recent_recommendation_feedback(recent_cutoff(store, now), now)
        # Retain the newest directed response for a recording within a scope.
        return list({(row['surface'], row['scope'], row['item_key']): row for row in reversed(rows)}.values())

    return store.memoized_read(('recent-recommendation-feedback', now), collect)


def radio_list_ids(store) -> list[str]:
    """#93v2 The radio playlists' ytm ids, resolved from settings (unset/empty skipped). This module
    and rec/layers.py read recent plays as TASTE EVIDENCE - what the user seems to want - and feed
    that reading back into the scores that steer what radio plays next (layers tilt every surface's
    ranking; play_facet_leans additionally graduates into PERMANENT weights daily via
    graduation.graduate_play_exposure). Without this exclusion, a radio pick counts as evidence the
    instant it plays, tilting the next pick toward whatever radio just queued: a closed loop that
    runs away from anything the user actually chose (live find: an artist with 5 plays, all today,
    all radio-queued, took over 3 of 7 picks). Machine-queued radio plays are therefore excluded
    from every plays-read that feeds the taste model; the user's OWN signals on those same tracks -
    skips, ratings, explicit steering - are unaffected, since those flow through their own channels.
    Other generated sources also retain their provenance/identity quarantine in the history reader
    and evidence_plays. Deliberate context choices enter through choice learning. Trends/stats keep
    counting plays (descriptive, a different pipeline)."""
    sessions = store.playlists.radio_playlist_ids()
    transient = sorted(store.transient_radio_playlists.copy())
    return list(dict.fromkeys(i for i in sessions + transient if i))


def evidence_plays(store, limit):
    """Recent plays that are actually EVIDENCE OF TASTE, for the transient model. Two filters, because
    a machine-chosen play can reach the store two different ways:

      · by provenance - a live play_event carrying a radio playlist's id (radio_list_ids above);
      · by identity - a song that exists only inside a quarantined generated playlist. The YTM
        history sync records plays a day at a time with NO playlist attribution, so a generated
        playlist played outside the live bridge arrives looking exactly like organic listening.
        Nothing about the row says where it came from - but the SONG says it: if the only reason
        it's in the library is that a generated playlist put it there, playing it says nothing you
        chose. (rec_eligibility.generated_only_keys, which training baskets already respect.)

    Pressing play on a generated playlist is a verdict on the PLAYLIST, not on each track the app
    picked for it. Rating one, or promoting the playlist out of the Generated group, is the
    deliberate act that makes it evidence - the rule generated_only_keys already states: plays don't
    lift the quarantine, promotion does. The user's own signals on those tracks (likes, dislikes,
    skips, steering) flow through their own channels and are untouched."""
    quarantined = store.rec_eligibility.generated_only_keys()
    return [(k, ts) for k, ts in
            store.history.recent_plays_with_ts(limit=limit, exclude_list_ids=radio_list_ids(store))
            if k not in quarantined]


def facets_for(store, keys) -> dict:
    """Map track identity_keys to the rec axes they carry:
    {'genre:<fam>': [keys...], 'era:<decade>': [keys...], 'artist:<name>': [keys...]}.
    A key contributes to at most one axis of each type (its own family / decade / artist)."""
    keys = list(dict.fromkeys(keys))
    if not keys:
        return {}
    if not getattr(store, '_read_only', False) and not store._lock.held_by_current_thread:
        with store.read_view() as reader:
            return facets_for(reader, keys)
    dao = store.rec_query
    genres, decades, artists = dao.track_genres(keys), dao.track_decades(keys), dao.track_artists(keys)
    axes = store.genres.feature_axes(genres.values())
    return facets_from_metadata(keys, genres, decades, artists, axes)


def facets_from_metadata(keys, genres, decades, artists, axes):
    """Resolve explicit metadata into axes, leaving unknown metadata absent."""
    families = {genre: axes.get(genre, ('', None))[0] for genre in set(genres.values())}
    subgenres = {genre: axes.get(genre, ('', None))[1] for genre in families}
    out: dict = {}
    for k in keys:
        if k in genres:
            fam = families[genres[k]]
            if fam:
                out.setdefault(f"genre:{fam}", []).append(k)
            sub = subgenres[genres[k]]
            if sub and sub != fam:
                out.setdefault(f"genre:{sub}", []).append(k)
        if k in decades:
            out.setdefault(f"era:{decades[k]}", []).append(k)
        if k in artists:
            out.setdefault(f"artist:{artists[k]}", []).append(k)
    return out


def prepared_facet_leans(store, now):
    """Share a brief, dependency-guarded signal capture across preparation retries."""
    return store.memoized_read(('prepared-facet-leans', int(now // PREPARED_LEANS_INTERVAL_S)),
                               lambda: facet_leans(store, now))


def facet_leans(store, now) -> dict:
    """{facet: signed strength}: the token view of the transient model. #85: every source decays on
    the wall clock (per-source half-life params); a month-old vibe tap is nearly gone even if the
    user never interacted again."""
    gp = rec_params.get_param
    # TODO: consider replacing the legacy 0–2 play/like strengths with a consistent scale.
    # This is an arbitrary tuning range: 0 disables the extra signal; 1 is not neutral.
    # Defaults are 0.30 (plays) / 0.45 (likes). Taste Model displays 0–100% of the range;
    # keep stored values and scoring unchanged unless a future cleanup migrates them together.
    play_w, like_w = gp(store, "play_transient_w"), gp(store, "like_transient_w")
    play_hl, mood_hl = gp(store, "play_halflife_d"), gp(store, "mood_halflife_d")
    like_hl = gp(store, "like_halflife_d")
    skip_w, skip_hl = gp(store, "skip_transient_w"), gp(store, "skip_halflife_d")
    events, plays, likes, skips = recent_sources(store, now)
    contributions = signed_contributions(now, events, plays, likes, skips,
        play_w=play_w, like_w=like_w, skip_w=skip_w,
        play_hl=play_hl, mood_hl=mood_hl, like_hl=like_hl, skip_hl=skip_hl)
    contributions.extend(([row['item_key']], .5 * decay_weight(now - row['created_at'], like_hl), False)
                         for row in recent_feedback(store, now) if row['kind'] == 'more')
    facets = facets_for(store, [key for keys, _, _ in contributions for key in keys])
    from yt_playlist.rec import choice_learning
    choices = []
    cutoff = recent_cutoff(store, now)
    for interest in choice_learning.contexts(store, now):
        if interest['format'] != 'mixes' or interest.get('descriptor_id') is None:
            continue
        weight = interest['layer_weights']['recent']
        if not weight or interest['at'] < cutoff:
            continue
        descriptor = store.rec_ledger.mix_descriptor(interest['descriptor_id'])
        if descriptor is None:
            continue
        families = {value[0] for value in store.genres.feature_axes(descriptor['genres']).values()}
        choices.append((families, weight))
    return facet_leans_from_inputs(contributions, facets, choices)


def signed_contributions(now, events, plays, likes, skips, *,
                         play_w, like_w, skip_w, play_hl, mood_hl, like_hl, skip_hl):
    """Decay detached event facts while preserving source polarity and artist suppression."""
    contributions = []

    def add(keys, signed, drop_artist=False):
        if not keys or signed == 0:
            return
        contributions.append((list(dict.fromkeys(keys)), signed, drop_artist))

    for ts, direction, keys in events:
        add(keys, direction * decay_weight(now - ts, mood_hl))
    for k, ts in plays:
        add([k], play_w * source_confidence(ts) * decay_weight(source_age(now, ts), play_hl))
    # Likes here are ACTION-provenance only (recent_liked_with_ts filters at the repo): a like
    # observed live has a real event time and belongs at the top of the model stack. A like the
    # library sync merely discovered has no reliable time (its created_at is first-sight), so it
    # never reaches the transient model; it shapes the permanent model only, via graduation.
    for k, ts in likes:
        add([k], like_w * decay_weight(now - ts, like_hl))
    # A skip is specific to the track and moment. It supplies no artist signal.
    for k, ts in skips:
        add([k], -skip_w * decay_weight(now - ts, skip_hl), drop_artist=True)
    return contributions


def facet_leans_from_inputs(contributions, facets, choices):
    """Blend detached events and broad mix choices without catalog or history access."""
    leans = {}
    # Capture metadata once for this snapshot, preserving each event's decay,
    # denominator, ordering and artist suppression during accumulation.
    by_key = {}
    for facet, keys in facets.items():
        for key in keys:
            by_key.setdefault(key, []).append(facet)
    for keys, signed, drop_artist in contributions:
        counts = {}
        for key in keys:
            for facet in by_key.get(key, ()):
                if not drop_artist or not facet.startswith('artist:'):
                    counts[facet] = counts.get(facet, 0) + 1
        for facet, count in counts.items():
            leans[facet] = leans.get(facet, 0.0) + signed * (count / len(keys))
    # Mix choices are broad raw-genre preferences, never synthetic plays or
    # centroids over whichever recordings happen to match today's catalog.
    choice_genres = {}
    for families, weight in choices:
        for family in families - {None, ''}:
            axis = 'genre:' + family
            choice_genres[axis] = max(choice_genres.get(axis, 0.), min(1., weight) / max(1, len(families)))
    for axis, strength in choice_genres.items():
        leans[axis] = leans.get(axis, 0.) + strength
    return leans


def play_facet_leans(store, now) -> dict:
    """The recent-plays slice of `facet_leans`, in isolation.

    `facet_leans` blends every transient source (mood, plays, likes and skips). The play-exposure
    graduation funnel (graduation.graduate_play_exposure) must graduate plays SPECIFICALLY, because
    likes / positive vibe taps already graduate at event time; negative signals must not graduate. So
    this returns only the positive, wall-clock-decayed push from recent plays (#85: per-event age, no
    rank, no separate staleness relax). When listening stops the recent window drains and each play
    ages out on its own, so this falls to {} over time, which is the funnel's natural off-switch.
    """
    gp = rec_params.get_param
    play_w = gp(store, "play_transient_w")
    play_hl = gp(store, "play_halflife_d")
    limit = gp(store, "recent_play_limit")
    plays = evidence_plays(store, limit)
    facets = facets_for(store, [k for k, _ in plays])
    return play_leans_from_inputs(plays, facets, now, weight=play_w, half_life_d=play_hl)


def play_leans_from_inputs(plays, facets, now, *, weight, half_life_d):
    leans = {}
    by_key = {}
    # Read metadata once for the window, retaining each event's decay and the
    # accumulation order used by the per-track calculation.
    for facet, keys in facets.items():
        for key in keys:
            by_key.setdefault(key, []).append(facet)
    for k, ts in plays:
        for f in by_key.get(k, ()):
            leans[f] = leans.get(f, 0.0) + weight * decay_weight(now - ts, half_life_d)
    return leans


def centroid_tilt(store, now, V, idx, *, consumed_plays=None):
    """Bounded embedding tilt from the unified transient stream: mood events, recent plays,
    recent likes (action provenance only, see recent_liked_with_ts), and recent skips (#84,
    negative), each wall-clock decayed by its own half-life (#85). None if quiet.

    Divide the decayed vector sum by the contributing sources' UNDECAYED absolute weights. Its
    norm is at most one, retains freshness, and falls when signals disagree. Normalizing by the
    decayed vector's norm would erase aging whenever the evidence direction stays the same.
    Missing/zero vectors and disabled sources contribute neither signal nor denominator.
    Callers use this magnitude directly, without another normalization or freshness factor.
    """
    gp = rec_params.get_param
    skip_hl, skip_w = gp(store, "skip_halflife_d"), gp(store, "skip_transient_w")
    events, plays, likes, skips = recent_sources(store, now)
    if consumed_plays:
        plays = [(key, stamp) for key, stamp in plays if (key, stamp) not in consumed_plays]
    events = list(events) + [(row['created_at'], .5 if row['kind'] == 'more' else -.5, [row['item_key']])
                             for row in recent_feedback(store, now)]
    if not any((events, plays, likes, skips)):
        return None
    mood_hl = gp(store, "mood_halflife_d")
    play_hl, play_w = gp(store, "play_halflife_d"), gp(store, "play_transient_w")
    like_hl, like_w = gp(store, "like_halflife_d"), gp(store, "like_transient_w")
    return tilt_from_inputs(now, V, idx, events, plays, likes, skips,
        mood_hl=mood_hl, play_hl=play_hl, play_w=play_w, like_hl=like_hl,
        like_w=like_w, skip_hl=skip_hl, skip_w=skip_w)


def tilt_from_inputs(now, V, idx, events, plays, likes, skips, *,
                     mood_hl, play_hl, play_w, like_hl, like_w, skip_hl, skip_w):
    """Bounded tilt; its undecayed denominator preserves aging and cancellation."""
    tilt = np.zeros(V.shape[1], dtype=np.float64)
    weight = 0.0
    for ts, direction, keys in events:
        rows = [idx[k] for k in keys if k in idx]
        if not rows:
            continue
        c = V[rows].mean(0)
        nrm = np.linalg.norm(c)
        if nrm == 0:
            continue
        tilt += direction * decay_weight(now - ts, mood_hl) * (c / nrm)
        weight += abs(direction)

    def _add_dir(pairs, w_base, half_life_d):
        # Add each track's own unit vector, scaled by its source weight (w_base) and wall-clock decay.
        nonlocal tilt, weight
        if w_base == 0:
            return
        for k, ts in pairs:
            if k not in idx:
                continue
            v = V[idx[k]]
            nrm = np.linalg.norm(v)
            if nrm == 0:
                continue
            tilt = tilt + w_base * source_confidence(ts) * decay_weight(source_age(now, ts), half_life_d) * (v / nrm)
            weight += abs(w_base)

    _add_dir(plays, play_w, play_hl)
    _add_dir(likes, like_w, like_hl)
    # #84: a skip subtracts the skipped track's own unit direction, pulling the tilt AWAY from it
    # (negative w_base reuses the same accumulation the positive sources use).
    _add_dir(skips, -skip_w, skip_hl)
    return tilt / weight if weight > 0 and np.any(tilt) else None


def audio_centroid_tilt(store, now, *, content_vecs=None, consumed_plays=None):
    """Bounded tilt in the audio-aware CONTENT vector space toward recent listening, so ranking can
    lean to the SOUND of what you have been playing (tempo / energy / mood), not just its genre/era/
    artist facets or its co-occurrence neighbours. The content-space sibling of `centroid_tilt`: same
    transient stream (mood events, recent plays, recent action-provenance likes), same recency
    weighting, but built from
    the persisted content vectors (embedding_vectors.load_content_vectors), which carry the z-scored audio block.

    Returns None when the content model is unbuilt or no recent track has a content vector, so a cold
    or quiet user is exactly as today. Audio is sparse and growing: tracks without a content vector
    simply do not contribute, so the tilt is defined by whatever covered tracks the user has played
    (graceful degradation). Decay is per-event and internal (#85); callers apply no external freshness
    factor, mirroring `centroid_tilt`. #84: recent skips contribute too, subtracting the skipped
    track's own unit direction, same weight/decay as `centroid_tilt`.

    This is the producer only. The scorer applies it (a content-space alignment term parallel to the
    collaborative `_apply_mood`); see the wiring note that ships with this change.
    """
    _ckeys, CV, cidx = vectors.load_content_vectors(store) if content_vecs is None else content_vecs
    if CV is None:
        return None
    if consumed_plays:
        return centroid_tilt(store, now, CV, cidx, consumed_plays=consumed_plays)
    return centroid_tilt(store, now, CV, cidx)
