"""Local recommendation logic. Pure functions over a Store (no web imports), like analysis.py."""
from dataclasses import dataclass, field

import math
import random
import statistics
import time
import zlib

import numpy as np

from yt_playlist.library import analysis
from yt_playlist.rec import embed, genre_map, journeys, rec_params, transient
from yt_playlist.rec.rec_dao import RecDao


class PlaylistTaste:
    """Play-weighted per-playlist taste model: each playlist is one taste *context* (its embedding
    centroid), weighted by how much you actually listen to it. Scoring a candidate against this
    rewards fit to the contexts you play — so a low-play playlist (the 'vacation with Dad' problem)
    can't drag in off-taste recommendations, and distinct high-play contexts aren't blurred into one
    average. Catch-all playlists (too big to be a coherent context) are excluded.
    """

    def __init__(self, titles, centroids, weights, pids=()):
        self.titles = list(titles)               # playlist titles, one per context
        self.centroids = centroids               # (n, dim) L2-normalised rows, or empty
        self.weights = weights                   # (n,) sums to 1, or empty
        self.pids = list(pids)                   # playlist ids, aligned with titles (for the viz)

    def __bool__(self):
        return len(self.titles) > 0

    def score(self, vec, top=3):
        """(total, [(playlist_title, contribution), ...]) for a candidate taste vector."""
        if not self.titles:
            return 0.0, []
        v = vec / (np.linalg.norm(vec) + 1e-9)
        contrib = self.weights * (self.centroids @ v)        # play-weighted cosine per context
        order = np.argsort(-contrib)[:top]
        because = [(self.titles[i], float(contrib[i])) for i in order if contrib[i] > 0]
        return float(contrib.sum()), because

    def score_all(self, V):
        """Per-context taste score for every row of V (N, dim) -> (N,). Vectorized."""
        if not self.titles or len(V) == 0:
            return np.zeros(len(V))
        Vn = V / (np.linalg.norm(V, axis=1, keepdims=True) + 1e-9)
        return self.weights @ (self.centroids @ Vn.T)        # (P,)·(P,N) -> (N,)


def playlist_taste(store, max_tracks=120) -> PlaylistTaste:
    """Build the per-playlist taste model from the embedding + listen history."""
    keys, V, idx = embed.load_vectors(store)
    if V is None:
        return PlaylistTaste([], np.zeros((0, 0)), np.zeros(0))
    stats = store.get_playlist_listen_stats()                # {pid: (last_ts, listen_count)}
    excluded = RecDao(store).excluded_playlist_ids()         # generated playlists don't shape taste
    titles, cents, ws, pids = [], [], [], []
    for p in store.get_playlists():
        if p.id in excluded:
            continue
        members = store.get_playlist_track_keys(p.id)
        if len(members) > max_tracks:                        # skip catch-alls — not a coherent context
            continue
        rows = [idx[k] for k in members if k in idx]
        if not rows:
            continue
        c = V[rows].mean(0)
        n = np.linalg.norm(c)
        if n == 0:
            continue
        titles.append(p.title)
        cents.append(c / n)
        ws.append(stats.get(p.id, (None, 0))[1] or 0)        # how much you listen to this playlist
        pids.append(p.id)
    if not titles:
        return PlaylistTaste([], np.zeros((0, 0)), np.zeros(0))
    w = np.asarray(ws, dtype=np.float64)
    w = w / w.sum() if w.sum() > 0 else np.full(len(titles), 1.0 / len(titles))   # uniform if no plays
    return PlaylistTaste(titles, np.asarray(cents), w, pids)


def genre_adjusted_scores(scores, genre_of, gweights):
    """Re-weight per-track taste scores by the user's per-genre-family preferences.

    `gweights` maps genre family -> weight (1.0 = neutral, 0 = mute, >1 = favor). Because raw taste
    scores can be negative (cosine), every score is first shifted to a common non-negative base, then
    scaled by its family's weight — so ordering stays well-defined regardless of sign: a muted family
    sinks to 0, a boosted one rises. Returns a new {key: score}; a pure no-op when all weights are
    neutral, so the default path is untouched.
    """
    if not scores or not gweights or all(w == 1.0 for w in gweights.values()):
        return scores
    smin = min(scores.values())
    eps = 1e-6
    return {k: (s - smin + eps) * gweights.get(genre_of.get(k), 1.0) for k, s in scores.items()}


def axis_adjusted_scores(scores, mult):
    """Re-weight taste scores by a precomputed per-key multiplier (generalizes genre weighting).

    Shift to a common non-negative base then scale, so ordering is well-defined for negative cosines.
    No-op when `mult` is falsy. Returns a new {key: score}.
    """
    if not scores or not mult or all(m == 1.0 for m in mult.values()):
        return scores
    smin = min(scores.values())
    eps = 1e-6
    return {k: (s - smin + eps) * mult.get(k, 1.0) for k, s in scores.items()}


def _axis_weights_for(store, keys, now=None):
    """{key: genre_w * era_w * artist_w}, where each axis weight is permanent x standing lean x the
    transient facet multiplier (live 'more/less this facet'). None if every factor is neutral."""
    w = store.get_weights()
    gw = {a[len("genre:"):]: v for a, v in w.items() if a.startswith("genre:")}
    ew = {a[len("era:"):]: v for a, v in w.items() if a.startswith("era:")}
    aw = {a[len("artist:"):]: v for a, v in w.items() if a.startswith("artist:")}
    leans = transient.facet_leans(store, now) if now is not None else {}
    standing = store.get_leans()
    perm_neutral = all(v == 1.0 for v in list(gw.values()) + list(ew.values()) + list(aw.values()))
    if perm_neutral and not leans and not standing:
        return None
    keys = list(keys)
    dao = RecDao(store)
    genres, decades, artists = dao.track_genres(keys), dao.track_decades(keys), dao.track_artists(keys)
    lo, hi = rec_params.GENRE_MIN, rec_params.GENRE_MAX

    def tm(token):
        return transient.facet_multiplier(leans.get(token, 0.0))

    def sl(token):
        return standing.get(token, 1.0)

    mult = {}
    for k in keys:
        fam = genre_map.family(genres[k]) if k in genres else None
        sub = genre_map.subgenre(genres[k]) if k in genres else None
        dec = decades.get(k)
        art = artists.get(k)
        gm = gw.get(fam, 1.0) * sl(f"genre:{fam}") * (tm(f"genre:{fam}") if fam else 1.0)
        if sub and sub != fam:
            gm *= gw.get(sub, 1.0) * sl(f"genre:{sub}") * tm(f"genre:{sub}")
        em = ew.get(dec, 1.0) * sl(f"era:{dec}") * (tm(f"era:{dec}") if dec else 1.0)
        am = aw.get(art, 1.0) * sl(f"artist:{art}") * (tm(f"artist:{art}") if art else 1.0)
        mult[k] = max(lo, min(hi, gm)) * max(lo, min(hi, em)) * max(lo, min(hi, am))
    return mult


def _apply_axis_weights(store, sims, now=None):
    """Re-weight a {key: taste-score} map by permanent preferences × the live transient facet leans."""
    mult = _axis_weights_for(store, list(sims), now=now)
    return sims if mult is None else axis_adjusted_scores(sims, mult)


def era_distribution(store) -> list:
    """Decades present in the library, by play-weighted share, most-prominent first."""
    dist = RecDao(store).era_play_distribution()
    total = sum(dist.values())
    if not total:
        return []
    return sorted(((d, c / total) for d, c in dist.items()), key=lambda x: -x[1])


def taste_fingerprint(store) -> dict:
    """Compact, legible 'you right now' for the Home header: top genre families, breadth, era lean.

    Each family/era carries its current steering weight so the header can render a draggable bar.
    Also includes 'effective' = permanent x standing lean, clamped to [GENRE_MIN, GENRE_MAX], which
    is what the slider value binds to (the bar shows what the user actually experiences).

    Pinned niches: any genre:<x> or era:<x> axis present in stored leans (set via /home/fingerprint/add)
    that is NOT already in the play-distribution-based lists is appended with share=0.0 so a searched,
    zero-play subgenre always renders as a steerable bar.
    """
    bd = taste_breadth(store)
    w = store.get_weights()
    families = [{"name": f, "share": share, "weight": w.get(f"genre:{f}", 1.0),
                 "effective": max(rec_params.GENRE_MIN, min(rec_params.GENRE_MAX,
                                  w.get(f"genre:{f}", 1.0) * store.get_lean(f"genre:{f}")))}
                for f, share in sorted(bd["families"].items(), key=lambda x: -x[1])]
    eras = [{"name": d, "share": share, "weight": w.get(f"era:{d}", 1.0),
             "effective": max(rec_params.GENRE_MIN, min(rec_params.GENRE_MAX,
                              w.get(f"era:{d}", 1.0) * store.get_lean(f"era:{d}")))}
            for d, share in era_distribution(store)]

    # Append pinned niches: axes stored in leans but not yet in the play-distribution lists.
    known_genre_names = {entry["name"] for entry in families}
    known_era_names = {entry["name"] for entry in eras}
    leans = store.get_leans()
    for axis, lean_val in leans.items():
        if axis.startswith("genre:"):
            name = axis[len("genre:"):]
            if name not in known_genre_names:
                weight = w.get(axis, 1.0)
                effective = max(rec_params.GENRE_MIN, min(rec_params.GENRE_MAX, weight * lean_val))
                families.append({"name": name, "share": 0.0, "weight": weight, "effective": effective})
                known_genre_names.add(name)
        elif axis.startswith("era:"):
            name = axis[len("era:"):]
            if name not in known_era_names:
                weight = w.get(axis, 1.0)
                effective = max(rec_params.GENRE_MIN, min(rec_params.GENRE_MAX, weight * lean_val))
                eras.append({"name": name, "share": 0.0, "weight": weight, "effective": effective})
                known_era_names.add(name)

    return {"families": families, "eras": eras, "breadth": bd["breadth"]}


def playlist_facets(store, playlist_id) -> dict:
    """The mix's facets for the transient feedback panel: the genres, eras, and tracks present, each
    with the identity_keys to tilt toward/away. Genres are by descending presence; eras chronological.
    """
    keys = store.get_playlist_track_keys(playlist_id)
    dao = RecDao(store)
    meta = store.tracks_by_keys(keys)
    genres, decades = dao.track_genres(keys), dao.track_decades(keys)
    fam_keys, era_keys = {}, {}
    for k in keys:
        if k in genres:
            fam_keys.setdefault(genre_map.family(genres[k]), []).append(k)
        if k in decades:
            era_keys.setdefault(decades[k], []).append(k)
    return {
        "genres": [{"name": f, "keys": ks} for f, ks in sorted(fam_keys.items(), key=lambda x: -len(x[1]))],
        "eras": [{"name": d, "keys": ks} for d, ks in sorted(era_keys.items())],
        "tracks": [{"key": k, "title": meta[k]["title"], "artist": meta[k]["artist"]}
                   for k in keys if k in meta],
    }


def playlist_mood_state(store, playlist_id, now) -> int:
    """+1/-1 if there's an active *whole-mix* mood for this playlist, else 0. Persists until you change
    it (the transient signal no longer expires on a clock)."""
    keys = set(store.get_playlist_track_keys(playlist_id))
    if not keys:
        return 0
    for _created, direction, mkeys in RecDao(store).recent_mood_events():   # newest-first
        if set(mkeys) == keys:                       # whole-mix feedback; first match is the latest
            return 1 if direction > 0 else -1
    return 0


def track_mood_states(store, now) -> dict:
    """Map identity_key -> +1/-1 for *per-track* mood signals (the "🔥 More / 🙅 Less like this" row
    levers), so a generated playlist can flag which rows you nudged. Those buttons seed a single key,
    so single-key events are exactly the per-track ones — whole-mix and facet tilts (many keys) are
    excluded. Persists until changed; newest-first, so the first event seen for a key is the latest."""
    out = {}
    for _created, direction, mkeys in RecDao(store).recent_mood_events():   # newest-first
        if len(mkeys) == 1 and mkeys[0] not in out:    # first match is the latest -> latest wins
            out[mkeys[0]] = 1 if direction > 0 else -1
    return out


def taste_breadth(store) -> dict:
    """How narrow vs eclectic this library is, from the entropy of its genre-family mix.

    breadth in [0,1]: ~0 = one-vibe (opera-only), ~1 = spread across many families. Computed over
    a *play-weighted* genre distribution, so a low-play context doesn't inflate your apparent breadth
    or register its genres as 'in palette' (the Clapton-leak fix). Spec §5.2.
    """
    fams: dict = {}
    for genre, c in RecDao(store).genre_play_distribution().items():
        fams[genre_map.family(genre)] = fams.get(genre_map.family(genre), 0) + c
    total = sum(fams.values())
    if total == 0 or len(fams) <= 1:
        return {"breadth": 0.0, "n_families": len(fams),
                "families": {f: 1.0 for f in fams}, "n_tagged": total}
    shares = {f: c / total for f, c in fams.items()}
    entropy = -sum(p * math.log(p) for p in shares.values())
    return {"breadth": entropy / math.log(len(fams)), "n_families": len(fams),
            "families": shares, "n_tagged": total}


def palette(store):
    """The library's genre-family palette + an out-of-palette penalty for a candidate genre.

    fit(genre) = 0 if its family is already present; otherwise the nearest-present-family
    distance scaled by breadth — 'absence-as-avoidance': a broad library that has never adopted
    a family penalizes it more (deliberate exclusion), a narrow one less (merely unexplored).
    Spec §5.3.
    """
    bd = taste_breadth(store)
    present = set(bd["families"])

    def fit(genre):
        fam = genre_map.family(genre)
        if not present or fam in present:
            return 0.0
        nearest = min(genre_map.family_distance(fam, p) for p in present)
        return nearest * (rec_params.get_param(store, "palette_absence_penalty") + bd["breadth"])

    return {"breadth": bd["breadth"], "present": present, "fit": fit}

SYNC_STALE_S = rec_params.SYNC_STALE_S   # highlight the Sync card after this (defined in rec_params)


def playlist_genre_diversity(store, playlist_id):
    """How genre-tight vs genre-wide a playlist is, via pairwise genre-map distances.

    Returns {min, max, median, n_tagged} over its tagged tracks, or None if fewer than two
    are tagged. median≈0 = tight (one vibe); median≈1 = eclectic. Spec §6.B.
    """
    genres = store.playlist_track_genres(playlist_id)
    if len(genres) < 2:
        return None
    dists = [genre_map.distance(genres[i], genres[j])
             for i in range(len(genres)) for j in range(i + 1, len(genres))]
    return {"min": min(dists), "max": max(dists),
            "median": statistics.median(dists), "n_tagged": len(genres)}


def genre_distance_fn(store, alpha=0.5):
    """A genre-distance function blending the static meta-genre map with this library's own
    co-occurrence: genres you repeatedly playlist together are pulled closer. alpha = static
    weight. Falls back to the static map for pairs you've never grouped. Spec §2.1/§5.3.
    """
    co = store.genre_cooccurrence()
    pairs, occ = co["pairs"], co["occ"]

    def dist(g1, g2):
        base = genre_map.distance(g1, g2)
        a, b = (g1, g2) if g1 <= g2 else (g2, g1)
        c = pairs.get((a, b), 0)
        if c == 0 or not occ.get(g1) or not occ.get(g2):
            return base
        jaccard = c / (occ[g1] + occ[g2] - c)
        return alpha * base + (1 - alpha) * (1 - jaccard)

    return dist


@dataclass
class ForYouItem:
    title: str
    artist: str
    album: str
    video_id: str | None
    thumbnail: str | None
    plays: int
    reason: str        # why this was recommended (human-readable)
    key: str = ""      # track identity_key, for feedback (dismiss/less/mute)
    lane: str = ""     # source lane (neighbourhood/rotation/deep_cut/comfort), for weighting
    genre: str = ""    # filled in at DJ-ordering time (attach_genres) so dj_order can do genre segues


def rotate_sample(items, size, epoch):
    """A stable-per-epoch random slice of `items` — how a Home list card 'regenerates with a random
    seed'. Within an erosion epoch (a window of erosion_view_cap views) the seed is fixed, so the
    card holds steady; the next epoch reseeds to a fresh set. Order within the slice is preserved
    (the pool stays ranked). Returns up to `size` items; a no-op when the pool already fits."""
    items = list(items)
    if len(items) <= size:
        return items
    rng = random.Random(epoch)
    return [items[i] for i in sorted(rng.sample(range(len(items)), size))]


def rotate_page(items, size, epoch):
    """Ordered rotation for a grid card (new artists / albums): show `size` items, advancing one page
    per epoch and wrapping once the pool is exhausted — so we cycle back through them rather than
    going empty. Returns up to `size` items (fewer only when the whole pool is smaller)."""
    items = list(items)
    n = len(items)
    if n == 0:
        return []
    start = (epoch * size) % n
    return [items[(start + i) % n] for i in range(min(size, n))]


# HOME CARD: "Wheelhouse" ("More in your wheelhouse") — deeper into what you already love.
# Internal lanes here are 'neighbourhood' + 'deep_cut'. When naming new code/vars, prefer the home
# heading "wheelhouse" over the legacy function name "for_you".
def for_you(store, now, limit=24) -> list[ForYouItem]:
    """Blended local recommendations from your taste model, interleaved and deduped, best-ranked
    first. Returns a deep, ranked pool; per-card rotation (a random epoch-seeded slice) happens at
    the surface, not here — for_you itself carries no anti-staleness state.

    Wheelhouse is your taste/genre model — not play-recency (that's the Comfort Listening card).
    Sources, strongest-available first:
      - taste neighbourhood: tracks near what you play most, re-ranked by your per-playlist taste
        and genre/era weights (falls back to plain rotation co-occurrence until the model is built)
      - deep cuts: the most-neglected track of each artist you play a lot
    """
    gp = rec_params.get_param
    pool = limit * gp(store, "candidate_pool_factor")   # fetch deeper than we show, so rotation has slack
    sources = []
    # The taste-embedding scores: per-playlist taste fit, tilted by the transient model (mood centroid
    # + genre/era/artist facet leans, staleness-gated). This single score drives both the neighbourhood
    # lane's candidate selection AND every lane's final ordering — no separate recency mechanism.
    sims = None
    if store.rec_vectors_count():
        pt = playlist_taste(store)
        keys, V, idx = embed.load_vectors(store)
        if pt and V is not None:
            allscores = _apply_mood(pt.score_all(V), store, now, V, idx)   # taste, tilted by transient mood
            sims = _apply_axis_weights(store, {keys[i]: float(allscores[i]) for i in range(len(keys))}, now)

    if sims is not None:
        # Neighbourhood lane: top tracks by taste×transient, excluding your most-played (so it's the
        # *neighbourhood* of your taste, not your hits).
        seeds = set(store.top_played_keys(limit=8))
        top = [k for k in sorted(sims, key=lambda k: -sims[k]) if k not in seeds][:pool]
        meta = store.tracks_by_keys(top)
        nbrs = [{"key": k, "plays": 0, **meta[k]} for k in top if k in meta]
        sources.append((nbrs, lambda r: "In your taste neighbourhood", "neighbourhood"))
    else:
        # Until the embedding model is built, fall back to plain rotation co-occurrence.
        sources.append((store.more_like_rotation(limit=pool),
                        lambda r: _rotation_reason(r["shared_playlists"]), "rotation"))
    sources.append((store.deep_cuts(limit=pool),
                    lambda r: f"A deep cut from {r['artist']}, who you play a lot", "deep_cut"))

    # Re-rank every lane's candidates by the same taste×transient score.
    if sims is not None:
        for rows, _, _ in sources:
            rows.sort(key=lambda r: -sims.get(r["key"], -1.0))

    weights = store.get_weights()
    # Never show these: dismissed/snoozed/muted, and anything already bundled into a generated
    # playlist (don't re-offer what you just saved). Anti-staleness is per-card rotation, not here.
    dao = RecDao(store)
    hard = store.suppressed_keys("for_you", now) | dao.generated_track_keys()
    muted = store.muted_artists()
    # weighted fair queuing: each lane gets turns ∝ its learned weight (default 1.0 => round-robin)
    queues = [[list(rows), reason, lane, weights.get(f"lane:{lane}", 1.0), 0]
              for rows, reason, lane in sources]
    seen: set = set()
    out: list[ForYouItem] = []
    while len(out) < limit:
        live = [q for q in queues if q[0]]
        if not live:
            break
        q = max(live, key=lambda q: q[3] / (q[4] + 1))   # weight / (taken + 1)
        rows, reason, lane, _, _ = q
        r = rows.pop(0)
        if r["key"] in seen or r["key"] in hard or r["artist"] in muted:
            seen.add(r["key"])
            continue
        seen.add(r["key"])
        q[4] += 1
        out.append(ForYouItem(
            title=r["title"], artist=r["artist"], album=r["album"], video_id=r["video_id"],
            thumbnail=r["thumbnail"], plays=r["plays"], reason=reason(r), key=r["key"], lane=lane))
    return out


# HOME CARD: "Comfort" ("Comfort listening") — most-played favourites that have gone quiet.
def comfort_listening(store, now, limit=24) -> list[ForYouItem]:
    """High-rotation favorites you haven't reached for lately — your comfort listening.

    Ranks your most-played tracks, demoted the more recently you've heard them (see
    store.comfort_candidates), so the card surfaces reliable favorites that have gone quiet rather
    than whatever's already in heavy rotation. Single-source, so no lane fair-queuing; it still
    honours the for_you suppression set (dismissed/snoozed/muted) and never re-offers a track
    already bundled into a generated playlist. No erosion: the recency demotion is its own freshness.
    """
    gp = rec_params.get_param
    pool = limit * gp(store, "candidate_pool_factor")
    rows = store.comfort_candidates(now, min_plays=gp(store, "comfort_min_plays"),
                                    recency_full_days=gp(store, "comfort_recency_full_days"), limit=pool)
    dao = RecDao(store)
    suppressed = store.suppressed_keys("for_you", now) | dao.generated_track_keys()
    muted = store.muted_artists()
    out: list[ForYouItem] = []
    for r in rows:
        if len(out) >= limit:
            break
        if r["key"] in suppressed or r["artist"] in muted:
            continue
        out.append(ForYouItem(
            title=r["title"], artist=r["artist"], album=r["album"], video_id=r["video_id"],
            thumbnail=r["thumbnail"], plays=r["plays"],
            reason="One of your most-played - you haven't reached for it lately",
            key=r["key"], lane="comfort"))
    return out


def rediscover_playlists(store, now, count=2, per=5, epoch=0, pool=8) -> list[dict]:
    """Spotlight real library playlists you haven't reached for lately, to nudge a rediscover.

    Ranks by *aggregate* track staleness — the median of when each track was last played, with
    never-played tracks counted as cold — so a playlist leads only when most of it has gone unplayed,
    not because one track was heard recently. Then rotates like the other Home cards: pages through
    the coldest `pool` playlists `count` at a time, advancing one page per rotation `epoch` (wrapping),
    so you cycle through only the genuinely-cold tail rather than ever drifting into warmer playlists.
    Highlights `per` tracks from each page as a teaser. Skips system playlists (Liked Music, Episodes
    for Later), Generated proto-playlists, and empties — none of those are a library playlist to revisit.
    """
    from yt_playlist.repos.rec_query import GENERATED_GROUP
    recency = store.get_playlist_track_recency()         # {pid: [per-track last-played ts | None, ...]}
    groups = store.get_playlist_groups()                 # ytm -> group name
    candidates = []
    for p in store.get_playlists():
        if (p.ytm_playlist_id in analysis.SYSTEM_PLAYLIST_IDS
                or groups.get(p.ytm_playlist_id) == GENERATED_GROUP
                or not p.track_count):
            continue
        # Never-played tracks count as cold: a 0.0 sentinel sorts older than any real timestamp.
        lasts = [t if t is not None else 0.0 for t in (recency.get(p.id) or [0.0])]
        candidates.append((p, statistics.median(lasts)))
    candidates.sort(key=lambda c: c[1])                  # coldest aggregate (oldest median) leads
    cold = candidates[:max(count, pool)]                 # rotate within the coldest tail only
    out = []
    for p, med in rotate_page(cold, count, epoch):       # rotate through the ranked cold pool
        out.append({"id": p.id, "ytm": p.ytm_playlist_id, "title": p.title,
                    "track_count": p.track_count, "thumbnail": p.thumbnail,
                    # the median is per-track; 0.0 means most tracks have never been played
                    "last_played": _ago(now - med) if med > 0 else None,
                    "tracks": store.playlist_tracks_detail(p.id)[:per]})
    return out


def rediscover_albums(store, now, count=3, epoch=0, pool=9) -> list[dict]:
    """Spotlight saved albums you haven't played lately, to nudge a revisit — the album cousin of
    rediscover_playlists. Ranks each saved album by its newest play across the album's tracks
    (never-played counts as coldest), then rotates through the coldest `pool` like the other Home
    cards: `count` at a time, advancing one page per rotation `epoch` (wrapping). Returns [] when
    nothing is saved. Tiles carry just metadata + thumbnail (no track teaser)."""
    saved = store.get_saved_albums()
    if not saved:
        return []
    recency = store.saved_albums_recency()               # {browse: newest play ts | None}
    # Never-played (None) sorts older than any real timestamp -> coldest leads.
    ranked = sorted(saved, key=lambda a: recency.get(a["browse"]) or 0.0)
    cold = ranked[:max(count, pool)]                      # rotate within the coldest tail only
    return [{"browse_id": a["browse"], "title": a["title"], "artist": a["artist"],
             "year": a["year"], "thumbnail": a["thumbnail"]}
            for a in rotate_page(cold, count, epoch)]


def taste_sample(store, now, limit=8, pool_factor=8) -> list[ForYouItem]:
    """A random *slice* of the tracks that match the current taste model — backs the Taste page's
    'refresh sample'. Unlike for_you (a deterministic ranking), this draws a deeper matching pool and
    samples from it, so every refresh is a new set even when the knobs are unchanged.
    """
    pool = for_you(store, now, limit=limit * pool_factor)
    if len(pool) <= limit:
        return pool
    return [pool[i] for i in sorted(random.sample(range(len(pool)), limit))]  # keep ranked order in-slice


def roll_recipe(store, model, seed=None, now=None) -> dict:
    """Roll a per-playlist theme. Preference-weighted by your play distribution × permanent axis
    weights × the live transient facet leans, so common facets come up often, a muted facet never
    rolls, and a fresh 'less house' makes house roll less in the very next generation."""
    rng = random.Random(seed)
    weights = store.get_weights()
    leans = transient.facet_leans(store, now) if now is not None else {}

    def pick(dist, prefix):
        items = [(k, share * weights.get(f"{prefix}:{k}", 1.0)
                     * transient.facet_multiplier(leans.get(f"{prefix}:{k}", 0.0)))
                 for k, share in dist.items()]
        items = [(k, w) for k, w in items if w > 0]
        if not items:
            return None
        r = rng.random() * sum(w for _, w in items)
        acc = 0.0
        for k, w in items:
            acc += w
            if r <= acc:
                return k
        return items[-1][0]

    genre = pick(taste_breadth(store)["families"], "genre")
    era = pick(dict(era_distribution(store)), "era")
    journey = pick(dict.fromkeys(journeys.JOURNEYS, 1.0), "journey") or "shuffle"
    facets = {}
    if genre:
        facets["genres"] = [genre]
    if era:
        facets["eras"] = [era]
    axis = {a: w for a, w in weights.items() if a.split(":", 1)[0] in ("genre", "era", "artist")}
    return {"model": model, "facets": facets, "params": {}, "journey": journey,
            "dj": {"stickiness": round(rng.random(), 2), "seed": rng.randint(0, 2**31 - 1)},
            "weights": axis}


def cluster_recipe(store, keep_keys, seed_labels=(), allow_families=(), journey="auto"):
    """Recipe + DJ-journey ordering for a saved Clusters mix (#15). model='cluster' gives the
    Generated playlist its own tunable type: the standard feedback panel applies, with a 'Made from'
    line built from the seeds you used, the genre families you restricted to (#29), and the genres /
    eras actually present; the chosen `journey` orders the tracks and makes the Flow lever real.

    `journey` is the user's DJ-Journey pick from the save bar; 'auto' (or anything unknown) ⇒ the
    'energy_arc' default. Unlike Home recipes this isn't theme-rolled — it just records what the
    cluster IS. Returns (recipe, ordered_keys); ordering is deterministic so a re-save lands the same."""
    journey = journey if journey in journeys.JOURNEYS else "energy_arc"
    keys = [k for k in dict.fromkeys(keep_keys) if k]
    dao = RecDao(store)
    genres, decades = dao.track_genres(keys), dao.track_decades(keys)
    lastp, plays = dao.track_last_played(keys), store.play_counts()
    meta = store.tracks_by_keys(keys)
    fam_count, era_count = {}, {}
    for k in keys:
        if k in genres:
            fam = genre_map.family(genres[k])
            fam_count[fam] = fam_count.get(fam, 0) + 1
        if k in decades:
            era_count[decades[k]] = era_count.get(decades[k], 0) + 1
    facets = {}
    if seed_labels:
        facets["artists"] = list(dict.fromkeys(seed_labels))[:4]
    fams = list(dict.fromkeys(allow_families)) or \
        [f for f, _ in sorted(fam_count.items(), key=lambda x: -x[1])[:3]]
    if fams:
        facets["genres"] = fams
    eras = [d for d, _ in sorted(era_count.items())][:3]            # chronological decades present
    if eras:
        facets["eras"] = eras
    seed = zlib.crc32("|".join(keys).encode()) & 0x7FFFFFFF

    def feat(k):
        g = genres.get(k, "")
        return {"artist": (meta.get(k) or {}).get("artist", ""), "genre": g,
                "energy": genre_map.energy(g), "decade": decades.get(k),
                "plays": plays.get(k, 0), "recency": lastp.get(k, 0.0)}

    order = journeys.journey_order(keys, journey, seed, feat)
    recipe = {"model": "cluster", "facets": facets, "journey": journey,
              "params": {"seeds": list(seed_labels), "genre_whitelist": list(allow_families)},
              "dj": {"stickiness": 0.0, "seed": seed}, "weights": {}}
    return recipe, order


def theme_filter(store, items, facets, limit=None):
    """Focus a model's candidate items on the rolled theme: items whose genre family / decade match
    the recipe come first, the rest follow (so the card still fills if the theme is thin). Items are
    ForYouItems (use .key). A no-op for un-keyed/un-tagged candidates (e.g. fresh songs)."""
    fam_want, era_want = set(facets.get("genres", [])), set(facets.get("eras", []))
    if not fam_want and not era_want:
        return list(items)
    keys = [i.key for i in items if getattr(i, "key", "")]
    dao = RecDao(store)
    genres, decades = dao.track_genres(keys), dao.track_decades(keys)

    def matches(i):
        fam = genre_map.family(genres[i.key]) if getattr(i, "key", "") in genres else None
        g_ok = (not fam_want) or (fam in fam_want)
        e_ok = (not era_want) or (decades.get(getattr(i, "key", "")) in era_want)
        return g_ok and e_ok

    hit = [i for i in items if matches(i)]
    miss = [i for i in items if not matches(i)]
    out = hit + miss
    return out[:limit] if limit else out


def versioned_title(store, prefix) -> str:
    """'{prefix} #{n}', where n increments over existing playlists sharing that prefix — so every
    regenerate of a type that day gets its own version (e.g. 'Fresh songs - June 21 2026 #2')."""
    n = 1 + sum(1 for p in store.get_playlists() if p.title.startswith(prefix))
    return f"{prefix} #{n}"


def _field(item, name):
    """Read a field from either a track dict (DOM/save path) or a ForYouItem (preview path)."""
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)


def attach_genres(store, items):
    """Fill each item's genre from the library (by identity_key) so dj_order can do genre segues.

    Works for both ForYouItem objects (preview) and plain track dicts (DOM/save); mutates in place
    and returns `items`. Without this, dj_order sees no genre and collapses to 'shuffle, but space
    same-artist' — no genre journey at all (the comfort-playlist bug). Untagged tracks stay '' (the
    segue just can't smooth across them), never an error.
    """
    from yt_playlist.util.matching import identity_key
    key_of = {id(it): (_field(it, "key")
                       or identity_key(_field(it, "title") or "", _field(it, "artist") or ""))
              for it in items}
    genres = RecDao(store).track_genres([k for k in key_of.values() if k])
    for it in items:
        g = genres.get(key_of[id(it)], "")
        if isinstance(it, dict):
            it["genre"] = g
        else:
            it.genre = g
    return items


def dj_order(tracks, stickiness=0.0, seed=0):
    """Order a chosen track set like a DJ. Start from a seeded shuffle, then greedily pick each next
    track by a lexicographic key: (a) never follow a track with the same artist unless every remaining
    track is that artist; (b) among the rest, place the artist with the MOST tracks left first — this
    schedules the heavy hitters early so they can't pile up at the end (the cause of same-artist
    clustering); (c) break ties by a `stickiness`-scaled genre segue (0 ≈ shuffle, 1 = careful genre
    transitions via the genre map). Guarantees no back-to-back same artist whenever that's feasible.
    Pure; returns a new list that is a permutation of `tracks`. Items may be track dicts or ForYouItem
    objects; both expose 'artist' and 'genre' (run attach_genres first so 'genre' is populated).
    """
    from collections import Counter
    items = list(tracks)
    if len(items) <= 2:
        return items
    rng = random.Random(seed)
    rng.shuffle(items)
    out = [items.pop(0)]
    while items:
        last = out[-1]
        remaining = Counter(_field(c, "artist") for c in items)

        def score(c):
            same = bool(_field(c, "artist")) and _field(c, "artist") == _field(last, "artist")
            lg, cg = _field(last, "genre") or "", _field(c, "genre") or ""
            gd = genre_map.distance(lg, cg) if (lg and cg) else 0.0
            seg = stickiness * gd + (1.0 - stickiness) * rng.random()
            # same-artist last (hard), then most-remaining-artist first (anti-pileup), then segue.
            return (same, -remaining[_field(c, "artist")], seg)

        items.sort(key=score)
        out.append(items.pop(0))
    return out


def _rotation_reason(n) -> str:
    return f"Sits with your favorites in {n} of your playlist{'s' if n != 1 else ''}"


def new_albums_from_favorites(ctx, limit_artists=10, limit=18) -> list[dict]:
    """Outward discovery (Phase 2): albums by your most-played artists that you don't already own
    or have saved. Uses the YTM client (network) — meant to run in the background worker, not per
    request. Degrades to [] with no client/network. Spec §1/§8 discovery."""
    from yt_playlist.web.routes.charts import _fetch_artist_info   # reuse the existing fetch
    store = ctx.store
    dao = RecDao(store)
    owned, saved = dao.owned_albums(), dao.saved_album_ids()
    out: list[dict] = []
    for a in store.top_artists(limit_artists):
        info = _fetch_artist_info(ctx, a["artist"])
        if not info:
            continue
        for alb in info.get("albums") or []:
            title = (alb.get("title") or "").strip()
            if not title or title.lower() in owned or alb.get("browse_id") in saved:
                continue
            out.append({"artist": a["artist"], "title": title, "year": alb.get("year"),
                        "browse_id": alb.get("browse_id"), "thumbnail": alb.get("thumbnail")})
            if len(out) >= limit:
                return out
    return out


# HOME CARD: "Fresh" ("Fresh songs") — tracks NOT in your collection yet (outward discovery).
def fresh_songs(ctx, limit=12) -> list[dict]:
    """Outward (Phase 2): songs from YTM radios seeded by your top tracks that you don't own.

    Uses the client (network) — runs in the background worker, never per request. Degrades to []
    with no client/network. limit matches the Home proto-card size (PROTO_SIZE) so the Fresh card
    can actually fill, rather than topping out short."""
    from yt_playlist.util.matching import identity_key
    from yt_playlist.util.thumbnails import best_thumb
    client = next(iter((ctx.client_provider() or {}).values()), None)
    if client is None:
        return []
    owned = RecDao(ctx.store).library_keys()
    out, seen = [], set()
    for t in ctx.store.top_tracks(6):
        vid = t.get("video_id")
        if not vid:
            continue
        try:
            radio = client.get_watch_playlist(vid) or {}
        except Exception:  # noqa: BLE001 - network/parse/missing-method -> skip this seed
            continue
        for r in radio.get("tracks") or []:
            v, title = r.get("videoId"), (r.get("title") or "").strip()
            artist = ((r.get("artists") or [{}])[0] or {}).get("name", "")
            if not v or not title:
                continue
            key = identity_key(title, artist)
            if key in owned or key in seen:
                continue
            seen.add(key)
            # watch-playlist tracks carry their art under `thumbnail` (singular); search/playlist
            # tracks use `thumbnails`. Try both so the fresh cards actually get cover art.
            out.append({"video_id": v, "title": title, "artist": artist,
                        "thumbnail": best_thumb(r.get("thumbnail") or r.get("thumbnails"))})
            if len(out) >= limit:
                return out
    return out


MOOD_ALPHA = 0.35   # how hard a mood event tilts the lanes, relative to the taste score


mood_tilt = transient.centroid_tilt   # back-compat: tests/callers use recommend.mood_tilt(store, now, V, idx)


def _apply_mood(scores, store, now, V, idx):
    """Blend the transient centroid tilt into per-track scores, scaled down as sync goes stale."""
    tilt = transient.centroid_tilt(store, now, V, idx)
    if tilt is None:
        return scores
    factor = transient.staleness_factor(store, now)
    if factor <= 0:
        return scores
    Vn = V / (np.linalg.norm(V, axis=1, keepdims=True) + 1e-9)
    return scores + MOOD_ALPHA * factor * (Vn @ tilt)


def apply_dislikes(store, status_map, now) -> None:
    """Fold a sync's per-track likeStatus into the model. A first-seen DISLIKE -> a long global
    suppression + a negative graduation contribution. A first-seen LIKE -> a positive transient
    signal (recency-captured) + a positive graduation contribution. A no-longer-disliked/liked track
    has its suppression/like cleared. NO direct permanent axis nudge — graduation owns that.
    Idempotent."""
    existing_dis = store.disliked_identity_keys()
    existing_like = set(store.recent_liked_keys())
    until = now + rec_params.get_param(store, "dislike_suppress_days") * 86400
    for key, status in status_map.items():
        if status == "DISLIKE":
            if key not in existing_dis and store.record_dislike(key, until, now):
                graduate_moods(store, [key], -1.0, now, source=rec_params.SOURCE_W_DISLIKE)
            if key in existing_like:
                store.clear_like(key)                       # a dislike supersedes a prior like
        elif status == "LIKE":
            if key not in existing_like and store.record_like(key, now):
                graduate_moods(store, [key], 1.0, now, source=rec_params.SOURCE_W_LIKE)
            if key in existing_dis:
                store.clear_dislike(key)                    # a like clears a prior dislike (preserved)
        elif status == "INDIFFERENT":
            if key in existing_dis:
                store.clear_dislike(key)
            if key in existing_like:
                store.clear_like(key)


def graduate_facet(store, axis, signed, now, source=1.0) -> None:
    """Accumulate one facet's signed event into the graduation ledger; when its running total
    crosses THEME_THRESHOLD, graduate it (a gentle permanent weight nudge, then a smooth reset).
    `source` is the signal's SOURCE_W_* weight (graduation speed). Model-only — NEVER suppresses."""
    score = store.bump_theme(axis, signed * source, now)
    if abs(score) >= rec_params.THEME_THRESHOLD:
        factor = rec_params.GRADUATE_UP if score > 0 else rec_params.GRADUATE_DOWN
        store.nudge_weight(axis, factor, lo=rec_params.GENRE_MIN, hi=rec_params.GENRE_MAX)
        store.discount_theme(axis, math.copysign(rec_params.THEME_THRESHOLD, score))


def graduate_moods(store, keys, signed, now, source=1.0) -> None:
    """Accumulate a transient-feeding event into the per-facet graduation ledger (presence-weighted),
    graduating each facet that crosses the threshold. `source` is the signal's SOURCE_W_* weight.
    Model-only — NEVER suppresses. `signed` carries intensity (±1, ±2 on 'a lot')."""
    facets = transient.facets_for(store, keys)
    if not facets:
        return
    n = len(set(keys)) or 1
    for axis, axis_keys in facets.items():
        graduate_facet(store, axis, signed * (len(axis_keys) / n), now, source=source)


def graduate_plays(store, keys, now) -> None:
    """Graduate just-played keys: weak per-play contribution (SOURCE_W_PLAY), with the whole session's
    play contribution capped at PLAY_GRAD_SESSION_CAP so a single binge cannot rewrite taste. Spreads
    the capped budget across the played facets proportionally to presence (counts duplicate plays)."""
    if not keys:
        return
    # Build axis -> play count from the original (possibly duplicate) keys list
    facets = transient.facets_for(store, keys)
    if not facets:
        return
    # Count how many plays each key appears (duplicates count as separate plays)
    from collections import Counter
    play_counts = Counter(keys)
    n = len(keys) or 1                                              # total plays this session
    raw = rec_params.SOURCE_W_PLAY * n                              # total play intensity this session
    scale = min(1.0, rec_params.PLAY_GRAD_SESSION_CAP / raw) if raw > 0 else 0.0
    for axis, axis_keys in facets.items():
        # Sum play counts across all unique keys mapped to this axis
        axis_play_count = sum(play_counts[k] for k in axis_keys)
        contribution = rec_params.SOURCE_W_PLAY * axis_play_count * scale
        # source already folded into `contribution`; pass source=1.0, signed=+contribution
        graduate_facet(store, axis, contribution, now, source=1.0)


def _utc_day(now) -> str:
    """UTC date string YYYY-MM-DD for a unix timestamp (held-day bucketing; deterministic for tests)."""
    return time.strftime("%Y-%m-%d", time.gmtime(now))


def graduate_slider_exposure(store, now) -> None:
    """Once per distinct held-day per axis, a held standing lean adds lean_magnitude * SOURCE_W_SLIDER
    to its graduation ledger. On crossing THEME_THRESHOLD: a permanent nudge_weight step, then migrate
    by dividing the lean by the actual permanent ratio so the displayed effective multiplier
    (permanent x lean) is conserved (sticky). Returning a slider to neutral (magnitude 0) stops all
    accrual."""
    today = _utc_day(now)
    for row in store.lean_rows():
        axis, value, last_day = row["axis"], row["value"], row["last_graduated_day"]
        if last_day == today:
            continue                                          # already exposed today
        magnitude = abs(value - 1.0)
        store.set_lean_graduated_day(axis, today)             # stamp the held-day either way
        if magnitude == 0.0:
            continue
        signed = math.copysign(magnitude * rec_params.SOURCE_W_SLIDER, value - 1.0)
        score = store.bump_theme(axis, signed, now)
        if abs(score) >= rec_params.THEME_THRESHOLD:
            factor = rec_params.GRADUATE_UP if score > 0 else rec_params.GRADUATE_DOWN
            old_perm = store.get_weights().get(axis, 1.0)
            new_perm = store.nudge_weight(axis, factor, lo=rec_params.GENRE_MIN, hi=rec_params.GENRE_MAX)
            ratio = (new_perm / old_perm) if old_perm > 0 else 1.0
            store.set_lean(axis, value / ratio, now)          # conserve: new_perm*(value/ratio) == old_perm*value
            store.discount_theme(axis, math.copysign(rec_params.THEME_THRESHOLD, score))


# HOME CARD: "Catalog" ("From your catalog") — own-but-under-played tracks, the edge of your palette.
# This powers the Catalog card, NOT a separate "Explore" surface; the internal lane name 'explore' is
# the source of the naming confusion. The home page dedups this pool against the Wheelhouse pool
# (see web/routes/home.py), so Catalog displays only what Wheelhouse didn't. When naming new
# code/vars, prefer the home heading "catalog" over "explore".
def explore_for_you(store, now, limit=24) -> list[ForYouItem]:
    """Catalog: your OWN under-played tracks, surfaced primarily by LACK OF PLAYS and *weighted*
    (never filtered) by the taste model. So a never-played track that fits your taste tops the card,
    an off-taste never-played track still appears (just lower), and a heavily-played track stays low
    no matter how well it fits — that's the Wheelhouse's job, not Catalog's. Empty until the
    embedding model is built. Distinct from Wheelhouse by signal (plays vs taste), not by a filter.
    """
    pt = playlist_taste(store)
    if not pt:
        return []
    keys, V, idx = embed.load_vectors(store)
    if V is None:
        return []
    # taste WEIGHT (taste-fit + transient mood + permanent×transient facets) — modulates, never filters
    scores = _apply_mood(pt.score_all(V), store, now, V, idx)
    sims = _apply_axis_weights(store, {keys[i]: float(scores[i]) for i in range(len(keys))}, now)
    smin = min(sims.values()) if sims else 0.0
    plays = store.play_counts()                                  # {key: count}; absent = never played
    # Catalog score = novelty(lack of plays, PRIMARY) × taste-fit weight (shifted positive so it only
    # ever scales, never zeroes — "weighted, not filtered").
    scored = {k: (1.0 / (1.0 + plays.get(k, 0))) * (sims.get(k, smin) - smin + 1e-6) for k in keys}
    order = sorted(scored, key=lambda k: -scored[k])
    dao = RecDao(store)
    suppressed = store.suppressed_keys("for_you", now) | dao.generated_track_keys()
    muted = store.muted_artists()
    meta = store.tracks_by_keys(order[:limit * 8])
    out: list[ForYouItem] = []
    for k in order[:limit * 8]:
        m = meta.get(k)
        if not m or k in suppressed or m["artist"] in muted:
            continue
        p = plays.get(k, 0)
        reason = "Never played — sits near your taste" if p == 0 else f"Barely played ({p}×) — worth another spin"
        out.append(ForYouItem(m["title"], m["artist"], m["album"], m["video_id"], m["thumbnail"],
                              p, reason, k, "explore"))
        if len(out) >= limit:
            break
    return out



def complete_playlist(store, playlist_id, limit=12, now=None) -> list[ForYouItem]:
    """Tracks you own that fit a given playlist but aren't in it yet.

    Uses the taste-embedding model (nearest to the playlist's centroid) once it's built;
    falls back to the artist/co-occurrence heuristic until then.
    """
    from collections import Counter
    members = store.get_playlist_track_keys(playlist_id)
    scope = str(playlist_id)
    suppressed = store.suppressed_keys("suggest", now or 0, scope=scope)
    muted = store.muted_artists()
    member_meta = store.tracks_by_keys(members)
    member_artists = {m["artist"] for m in member_meta.values()}
    # Spread `limit` suggestions across the playlist's distinct artists (>=2 each) so a tightly-
    # clustered artist can't flood an eclectic playlist's completion (the '529 repeats' bug). A
    # single-artist playlist has 1 distinct artist, so the cap is the whole limit — it still gets
    # plenty of that artist.
    distinct = len({m["artist"] for m in member_meta.values()}) or 1
    per_artist_cap = max(2, round(limit / distinct))

    def keep(key, artist):
        return key not in suppressed and artist not in muted

    if store.rec_vectors_count() and members:
        nbrs = embed.centroid_neighbors(store, list(members), topn=limit * 8, exclude=members)
        if nbrs:
            meta = store.tracks_by_keys([k for k, _ in nbrs])
            out, taken = [], Counter()
            for k, _ in nbrs:
                m = meta.get(k)
                if not m or not keep(k, m["artist"]) or taken[m["artist"]] >= per_artist_cap:
                    continue
                taken[m["artist"]] += 1
                reason = (f"More from {m['artist']}, already here" if m["artist"] in member_artists
                          else "Matches the sound of this playlist")
                out.append(ForYouItem(m["title"], m["artist"], m["album"], m["video_id"],
                                      m["thumbnail"], 0, reason, k))
                if len(out) >= limit:
                    break
            if out:                 # if every embedding neighbor was muted/suppressed, don't return
                return out          # an empty list — fall through to the co-occurrence heuristic


    out, taken = [], Counter()
    for r in store.complete_playlist(playlist_id, limit=limit * 8):
        if not keep(r["key"], r["artist"]) or taken[r["artist"]] >= per_artist_cap:
            continue
        taken[r["artist"]] += 1
        if r["same_artist"] and r["cooc"]:
            reason = f"By {r['artist']} (already here), and in {r['cooc']} related playlist(s)"
        elif r["same_artist"]:
            reason = f"More from {r['artist']}, already in this playlist"
        else:
            reason = f"Sits with these tracks in {r['cooc']} of your playlists"
        out.append(ForYouItem(
            title=r["title"], artist=r["artist"], album=r["album"], video_id=r["video_id"],
            thumbnail=r["thumbnail"], plays=0, reason=reason, key=r["key"]))
        if len(out) >= limit:
            break
    return out


@dataclass
class SyncStatus:
    last_synced_ago: str | None   # None if never synced
    stale: bool                   # never synced, or older than SYNC_STALE_S
    message: str | None           # highlight copy when stale, else None
    urgent: bool = False          # stale enough that the transient model is actively decaying


def sync_status(store, now) -> SyncStatus:
    # "Last synced" reflects the most recent sync of EITHER kind: a quick plays/auto sync keeps your
    # plays current just as a full sync does, so the badge must not claim you synced longer ago than
    # you actually did. Staleness rides the same most-recent stamp — recent plays = not stale.
    stamps = [float(s) for s in (store.get_setting("last_sync_at"),
                                 store.get_setting("last_plays_sync_at")) if s is not None]
    if not stamps:
        return SyncStatus(None, True, "Sync to pull in your library and recommendations.")
    age = now - max(stamps)
    if age > SYNC_STALE_S:
        if age > SYNC_STALE_S + rec_params.STALE_DECAY_HALFLIFE_D * 86400:
            return SyncStatus(_ago(age), True,
                              f"We haven't seen your plays in {_ago(age)}. Your recommendations are "
                              "drifting. Sync now.", urgent=True)
        return SyncStatus(_ago(age), True, "It's been a while. Sync to refresh.")
    return SyncStatus(_ago(age), False, None)


@dataclass
class ActionItem:
    kind: str          # "auth" | "cleanup" | "enrich"
    severity: str      # "high" | "med" | "low"
    title: str
    detail: str
    cta_label: str | None
    cta_href: str | None
    thumbnail: str | None = None
    thumbnails: list = field(default_factory=list)   # 0..2 covers for cards that show several playlists
    key: str = ""      # stable id for dismiss/snooze (e.g. 'enrich:12', 'cleanup:all')
    note: str = ""     # one-line orienting summary for the card (count + why); detail is the full text
    badge: str = ""    # tiny count chip shown beside the CTA (the number); detail is its tooltip


def _ago(seconds) -> str:
    days = int(seconds // 86400)
    if days >= 1:
        return f"{days} day{'s' if days != 1 else ''} ago"
    hours = int(seconds // 3600)
    if hours >= 1:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    minutes = int(seconds // 60)
    if minutes >= 1:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    return "just now"


CLEANUP_SURFACE = "cleanup"


def refresh_cleanup(store, now=None) -> dict:
    """Recompute the playlist-cleanup summary and cache it as a rec proposal (last-good serving).

    This is the ONLY place the heavy O(n²) cleanup scan runs for the home card: the rec worker calls
    it on every rebuild (so it tracks the playlist changes a sync brings in) and the /cleanup page
    calls it after every edit (its mutations HX-Refresh back through the GET). take_action then just
    reads the cached number — the home page never pays for the scan."""
    payload = analysis.cleanup_summary(store).as_payload()
    store.put_proposals(CLEANUP_SURFACE, payload, now)
    return payload


def take_action(store, now, auth_expired) -> list[ActionItem]:
    """Cards for things that genuinely need attention. Empty list = render nothing.

    Honors per-card snooze: an alert dismissed by the user stays hidden until its cooldown.
    """
    snoozed = store.suppressed_keys("alert", now)
    items: list[ActionItem] = []
    for label in auth_expired.values():
        items.append(ActionItem(
            "auth", "high", f"Re-authenticate {label}",
            "YouTube session expired - sync and recommendations are stale until you reconnect.",
            "Re-authenticate", "/setup", key=f"auth:{label}",
            note="Session expired - sync is stalled", badge="!"))

    # Read the cached summary the rec worker / cleanup page materialize — never scan on home load.
    cleanup = store.get_proposals(CLEANUP_SURFACE) or {}
    n = cleanup.get("count", 0)
    if n:
        items.append(ActionItem(
            "cleanup", "low", "Playlist cleanups",
            f"{n} playlist(s) look like duplicates, overlaps, or clutter - review and tidy them up "
            "on the cleanup page.",
            "Review", "/cleanup", thumbnails=cleanup.get("thumbnails", []), key="cleanup:all",
            note="Duplicates, overlaps & clutter to review", badge=str(n)))

    # Enrichment cards: playlists and saved albums, capped at 3 TOTAL (most-played playlists first,
    # then gappiest albums) so the section stays a tight, single row rather than a flood.
    enrich: list[ActionItem] = []
    for e in store.enrichment_candidates(limit=3):
        enrich.append(ActionItem(
            "enrich", "low", e["title"],
            f"{e['gaps']} of {e['total']} tracks are missing genre tags - and it's one of your "
            f"most-played playlists ({e['plays']} plays). Enriching it sharpens recommendations, "
            "since recs lean on genre and year.",
            "Enrich", f"/playlist/{e['id']}?enrich=1", thumbnail=e["thumbnail"], key=f"enrich:{e['id']}",
            badge=f"{e['gaps']}/{e['total']}"))
    for e in store.album_enrichment_candidates(limit=3):
        enrich.append(ActionItem(
            "enrich", "low", e["title"],
            f"{e['gaps']} of {e['total']} tracks on this saved album are missing genre tags. "
            "Enriching it sharpens recommendations, since the model now leans on these tracks too.",
            "Enrich", f"/album?browse={e['browse_id']}&enrich=1", thumbnail=e["thumbnail"],
            key=f"enrich-album:{e['browse_id']}", badge=f"{e['gaps']}/{e['total']}"))
    items += [i for i in enrich if i.key not in snoozed][:3]

    return [i for i in items if i.key not in snoozed]
