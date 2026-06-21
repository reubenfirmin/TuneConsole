"""Local recommendation logic. Pure functions over a Store (no web imports), like analysis.py."""
from dataclasses import dataclass

import math
import random
import statistics

import numpy as np

from yt_playlist import analysis, embed, genre_map, rec_params
from yt_playlist.rec_dao import RecDao


class PlaylistTaste:
    """Play-weighted per-playlist taste model: each playlist is one taste *context* (its embedding
    centroid), weighted by how much you actually listen to it. Scoring a candidate against this
    rewards fit to the contexts you play — so a low-play playlist (the 'vacation with Dad' problem)
    can't drag in off-taste recommendations, and distinct high-play contexts aren't blurred into one
    average. Catch-all playlists (too big to be a coherent context) are excluded.
    """

    def __init__(self, titles, centroids, weights):
        self.titles = list(titles)               # playlist titles, one per context
        self.centroids = centroids               # (n, dim) L2-normalised rows, or empty
        self.weights = weights                   # (n,) sums to 1, or empty

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
    titles, cents, ws = [], [], []
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
    if not titles:
        return PlaylistTaste([], np.zeros((0, 0)), np.zeros(0))
    w = np.asarray(ws, dtype=np.float64)
    w = w / w.sum() if w.sum() > 0 else np.full(len(titles), 1.0 / len(titles))   # uniform if no plays
    return PlaylistTaste(titles, np.asarray(cents), w)


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


def _axis_weights_for(store, keys):
    """{key: genre_w * era_w * artist_w} from rec_weights, or None if every axis is neutral."""
    w = store.get_weights()
    gw = {a[len("genre:"):]: v for a, v in w.items() if a.startswith("genre:")}
    ew = {a[len("era:"):]: v for a, v in w.items() if a.startswith("era:")}
    aw = {a[len("artist:"):]: v for a, v in w.items() if a.startswith("artist:")}
    if all(v == 1.0 for v in list(gw.values()) + list(ew.values()) + list(aw.values())):
        return None
    keys = list(keys)
    dao = RecDao(store)
    genres = dao.track_genres(keys)
    decades = dao.track_decades(keys)
    artists = dao.track_artists(keys)
    mult = {}
    for k in keys:
        fam = genre_map.family(genres[k]) if k in genres else None
        mult[k] = (gw.get(fam, 1.0) * ew.get(decades.get(k), 1.0) * aw.get(artists.get(k), 1.0))
    return mult


def _apply_axis_weights(store, sims):
    """Re-weight a {key: taste-score} map by the user's genre/era/artist preferences (no-op if neutral)."""
    mult = _axis_weights_for(store, list(sims))
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
    """
    bd = taste_breadth(store)
    w = store.get_weights()
    families = [{"name": f, "share": share, "weight": w.get(f"genre:{f}", 1.0)}
                for f, share in sorted(bd["families"].items(), key=lambda x: -x[1])]
    eras = [{"name": d, "share": share, "weight": w.get(f"era:{d}", 1.0)}
            for d, share in era_distribution(store)]
    return {"families": families, "eras": eras, "breadth": bd["breadth"]}


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

SYNC_STALE_S = 24 * 3600   # highlight the Sync card after 24h


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
    lane: str = ""     # source lane (resurface/neighbourhood/rotation/deep_cut), for weighting


def for_you(store, now, limit=24, erode=True) -> list[ForYouItem]:
    """Blended local recommendations from your taste model, interleaved and deduped.

    Wheelhouse is your taste/genre model — not play-recency (that's the Comfort Listening card).
    Sources, strongest-available first:
      - taste neighbourhood: tracks near what you play most, re-ranked by your per-playlist taste
        and genre/era weights (falls back to plain rotation co-occurrence until the model is built)
      - deep cuts: the most-neglected track of each artist you play a lot

    erode=True applies anti-staleness (hide items shown a lot lately) for the live feed. The Taste
    page preview passes erode=False so it shows the model's true ranking — otherwise erosion masks
    the effect of the knobs you're tuning.
    """
    gp = rec_params.get_param
    pool = limit * gp(store, "candidate_pool_factor")   # fetch deeper than we show, so erosion can rotate
    sources = []
    # the taste-embedding lane: tracks in the neighbourhood of what you play most.
    # Falls back to the plain co-occurrence query until the model has been built.
    nbrs = _taste_neighbourhood(store, pool, now) if store.rec_vectors_count() else None
    if nbrs:
        sources.append((nbrs, lambda r: "In your taste neighbourhood", "neighbourhood"))
    else:
        sources.append((store.more_like_rotation(limit=pool),
                        lambda r: _rotation_reason(r["shared_playlists"]), "rotation"))
    sources.append((store.deep_cuts(limit=pool),
                    lambda r: f"A deep cut from {r['artist']}, who you play a lot", "deep_cut"))

    # Tier-2 refinement: re-rank every lane's candidates by your play-weighted per-playlist taste,
    # so the strongest-fitting items rise within each lane (no single blurred centroid).
    if store.rec_vectors_count():
        pt = playlist_taste(store)
        keys, V, idx = embed.load_vectors(store)
        if pt and V is not None:
            allscores = _apply_mood(pt.score_all(V), store, now, V, idx)   # tilt by current mood
            sims = {keys[i]: float(allscores[i]) for i in range(len(keys))}
            sims = _apply_axis_weights(store, sims)                         # favor/mute genre·era·artist
            for rows, _, _ in sources:
                rows.sort(key=lambda r: -sims.get(r["key"], -1.0))

    weights = store.get_weights()
    # suppress dismissed/snoozed/muted, eroded items (shown enough lately), and anything already
    # bundled into a generated playlist (don't re-offer what you just saved) — anti-staleness
    dao = RecDao(store)
    suppressed = store.suppressed_keys("for_you", now) | dao.generated_track_keys()
    if erode:   # anti-staleness for the live feed; the taste-page preview turns this off
        suppressed |= dao.eroded_keys("for_you", now, view_cap=gp(store, "erosion_view_cap"),
                                      cooldown_days=gp(store, "erosion_cooldown_days"))
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
        if r["key"] in seen or r["key"] in suppressed or r["artist"] in muted:
            seen.add(r["key"])
            continue
        seen.add(r["key"])
        q[4] += 1
        out.append(ForYouItem(
            title=r["title"], artist=r["artist"], album=r["album"], video_id=r["video_id"],
            thumbnail=r["thumbnail"], plays=r["plays"], reason=reason(r), key=r["key"], lane=lane))
    return out


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
            reason="One of your most-played — you haven't reached for it lately",
            key=r["key"], lane="comfort"))
    return out


def taste_sample(store, now, limit=8, pool_factor=8) -> list[ForYouItem]:
    """A random *slice* of the tracks that match the current taste model — backs the Taste page's
    'refresh sample'. Unlike for_you (a deterministic ranking), this draws a deeper matching pool and
    samples from it, so every refresh is a new set even when the knobs are unchanged. erode=False:
    judge the model's true fit, not the anti-staleness-filtered live feed.
    """
    pool = for_you(store, now, limit=limit * pool_factor, erode=False)
    if len(pool) <= limit:
        return pool
    return [pool[i] for i in sorted(random.sample(range(len(pool)), limit))]  # keep ranked order in-slice


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


def fresh_songs(ctx, limit=10) -> list[dict]:
    """Outward (Phase 2): songs from YTM radios seeded by your top tracks that you don't own.

    Uses the client (network) — runs in the background worker, never per request. Degrades to []
    with no client/network. Spec §8 '10 fresh songs not in your library'."""
    from yt_playlist.matching import identity_key
    from yt_playlist.thumbnails import best_thumb
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


def auto_playlists(store, k=16, min_size=10, max_proposals=6) -> list[dict]:
    """Cluster the taste-embedding space into coherent groups and propose the ones that aren't
    already a playlist. Each proposal: {label, size, keys, sample, tracks} — `tracks` are the full
    saveable track dicts, so a proposal can be turned into a real playlist in one click. Spec §8."""
    clusters = embed.cluster(store, k)
    if not clusters:
        return []
    dao = RecDao(store)
    excluded = dao.excluded_playlist_ids()                   # a generated playlist isn't "already a playlist"
    existing = [set(store.get_playlist_track_keys(p.id)) for p in store.get_playlists() if p.id not in excluded]
    existing = [e for e in existing if e]
    props = []
    for keys in clusters.values():
        if len(keys) < min_size:
            continue
        ks = set(keys)
        if any(len(ks & e) / len(ks) > 0.6 for e in existing):   # already basically a playlist
            continue
        meta = store.tracks_by_keys(keys)
        tracks = [{"video_id": meta[k]["video_id"], "title": meta[k]["title"],
                   "artist": meta[k]["artist"], "album": meta[k].get("album", ""),
                   "thumbnail": meta[k]["thumbnail"]}
                  for k in keys if k in meta and meta[k].get("video_id")]
        props.append({
            "label": _cluster_label(dao, keys, meta),
            "size": len(keys),
            "keys": list(ks),
            "sample": [meta[k] for k in keys if k in meta][:6],
            "tracks": tracks,
        })
    props.sort(key=lambda p: -p["size"])
    return props[:max_proposals]


def _cluster_label(dao, keys, meta):
    """Name a cluster by its dominant genre family (if tagged) plus a couple of artists."""
    fams: dict = {}
    for g in dao.track_genres(keys).values():
        fam = genre_map.family(g)
        if not fam.startswith("other:"):
            fams[fam] = fams.get(fam, 0) + 1
    artists = {}
    for k in keys:
        a = (meta.get(k) or {}).get("artist")
        if a:
            artists[a] = artists.get(a, 0) + 1
    top_artists = [a for a, _ in sorted(artists.items(), key=lambda x: -x[1])[:2]]
    fam = max(fams, key=fams.get).replace("-", " ").title() if fams else "Mixed"
    return f"{fam} · incl. {', '.join(top_artists)}" if top_artists else fam


MOOD_ALPHA = 0.35   # how hard a mood event tilts the lanes, relative to the taste score


def mood_tilt(store, now, V, idx, half_life_h=3.0, window_h=8.0):
    """A transient, decaying direction in embedding space from recent mood feedback — NOT permanent
    taste. Each event contributes sign x time-decay x its seed-playlist centroid; the summed,
    normalized vector tilts the lanes for a few hours, then fades to nothing. None if quiet."""
    events = RecDao(store).active_mood(now, window_h)
    if not events:
        return None
    tilt = np.zeros(V.shape[1], dtype=np.float64)
    for created, direction, keys in events:
        rows = [idx[k] for k in keys if k in idx]
        if not rows:
            continue
        c = V[rows].mean(0)
        n = np.linalg.norm(c)
        if n == 0:
            continue
        age_h = max(0.0, (now - created) / 3600.0)
        tilt += direction * (0.5 ** (age_h / half_life_h)) * (c / n)   # decay toward zero with age
    n = np.linalg.norm(tilt)
    return tilt / n if n > 0 else None


def _apply_mood(scores, store, now, V, idx):
    """Blend the transient mood tilt into a per-track score vector (in place-safe; returns new)."""
    tilt = mood_tilt(store, now, V, idx)
    if tilt is None:
        return scores
    Vn = V / (np.linalg.norm(V, axis=1, keepdims=True) + 1e-9)
    return scores + MOOD_ALPHA * (Vn @ tilt)


def explore_for_you(store, now, limit=24) -> list[ForYouItem]:
    """The 'try something new' lane: tracks near your taste but novel — sitting close to your
    centroid yet by artists you *don't* play much. The edge of your palette, not the centre.
    Empty until the embedding model is built. Spec §5.4/§5.5.
    """
    pt = playlist_taste(store)
    if not pt:
        return []
    keys, V, idx = embed.load_vectors(store)
    scores = _apply_mood(pt.score_all(V), store, now, V, idx)   # taste fit, tilted by current mood
    order = np.argsort(-scores)
    familiar = {a["artist"] for a in store.top_artists(rec_params.get_param(store, "explore_top_artists"))}
    dao = RecDao(store)
    suppressed = (store.suppressed_keys("for_you", now)
                  | dao.eroded_keys("explore", now, view_cap=rec_params.get_param(store, "erosion_view_cap"),
                                    cooldown_days=rec_params.get_param(store, "erosion_cooldown_days"))
                  | dao.generated_track_keys())                 # don't re-offer saved-proto tracks
    muted = store.muted_artists()
    cand = [keys[i] for i in order[:limit * 12]]
    meta = store.tracks_by_keys(cand)
    out: list[ForYouItem] = []
    for k in cand:
        m = meta.get(k)
        if not m or k in suppressed or m["artist"] in muted or m["artist"] in familiar:
            continue
        out.append(ForYouItem(m["title"], m["artist"], m["album"], m["video_id"], m["thumbnail"],
                              0, "New to you — sits near your taste", k, "explore"))
        if len(out) >= limit:
            break
    return out


def _taste_neighbourhood(store, limit, now=None):
    """Tracks scoring high on your play-weighted per-playlist taste (slow, blur-free — distinct
    high-play contexts stay distinct), tilted toward your *recent* plays (fast/mood). Spec §5.1."""
    pt = playlist_taste(store)
    if not pt:
        return None
    keys, V, idx = embed.load_vectors(store)
    scores = pt.score_all(V)                                  # per-context taste (slow)
    if now is not None:
        win_h = rec_params.get_param(store, "recent_mood_window_hours")
        n_recent = rec_params.get_param(store, "recent_mood_tracks")
        recent = store.recent_keys_ordered(now - win_h * 3600.0, limit=n_recent)  # latest plays, in order
        mood = embed._centroid(V, idx, [(recent, 1.0)]) if recent else None
        if mood is not None:
            ratio = rec_params.get_param(store, "neighbourhood_taste_ratio")
            Vn = V / (np.linalg.norm(V, axis=1, keepdims=True) + 1e-9)
            scores = ratio * scores + (1.0 - ratio) * (Vn @ mood)   # taste vs. recent mood
    seeds = set(store.top_played_keys(limit=8))
    order = np.argsort(-scores)
    top = [keys[i] for i in order if keys[i] not in seeds][:limit]
    meta = store.tracks_by_keys(top)
    return [{"key": k, "plays": 0, **meta[k]} for k in top if k in meta]


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


def sync_status(store, now) -> SyncStatus:
    last = store.get_setting("last_sync_at")
    if last is None:
        return SyncStatus(None, True, "Sync to pull in your library and recommendations.")
    age = now - float(last)
    if age > SYNC_STALE_S:
        return SyncStatus(_ago(age), True, "It's been a while — sync to refresh.")
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
    key: str = ""      # stable id for dismiss/snooze (e.g. 'enrich:12', 'cleanup:empty')
    note: str = ""     # one-line orienting summary for the card (count + why); detail is the full text
    badge: str = ""    # tiny count chip shown beside the CTA (the number); detail is its tooltip


def _ago(seconds) -> str:
    days = int(seconds // 86400)
    if days >= 1:
        return f"{days} day{'s' if days != 1 else ''} ago"
    hours = int(seconds // 3600)
    if hours >= 1:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    return "just now"


def take_action(store, now, auth_expired) -> list[ActionItem]:
    """Cards for things that genuinely need attention. Empty list = render nothing.

    Honors per-card snooze: an alert dismissed by the user stays hidden until its cooldown.
    """
    snoozed = store.suppressed_keys("alert", now)
    items: list[ActionItem] = []
    for label in auth_expired.values():
        items.append(ActionItem(
            "auth", "high", f"Re-authenticate {label}",
            "YouTube session expired — sync and recommendations are stale until you reconnect.",
            "Re-authenticate", "/setup", key=f"auth:{label}",
            note="Session expired — sync is stalled", badge="!"))

    empties = analysis.find_empty_playlists(store)
    if empties:
        items.append(ActionItem(
            "cleanup", "low", "Empty playlists",
            f"{len(empties)} empty playlist(s) clutter your library — review and remove them.",
            "Review", "/cleanup", key="cleanup:empty", badge=str(len(empties))))

    dupes = analysis.find_near_duplicate_groups(store)
    if dupes:
        items.append(ActionItem(
            "cleanup", "low", "Near-duplicate playlists",
            f"{len(dupes)} group(s) of playlists heavily overlap — review them for merges.",
            "Review", "/cleanup", key="cleanup:dupes", badge=str(len(dupes))))

    # Enrichment cards: playlists and saved albums, capped at 3 TOTAL (most-played playlists first,
    # then gappiest albums) so the section stays a tight, single row rather than a flood.
    enrich: list[ActionItem] = []
    for e in store.enrichment_candidates(limit=3):
        enrich.append(ActionItem(
            "enrich", "low", e["title"],
            f"{e['gaps']} of {e['total']} tracks are missing genre tags — and it's one of your "
            f"most-played playlists ({e['plays']} plays). Enriching it sharpens recommendations, "
            "since recs lean on genre and year.",
            "Enrich", f"/playlist/{e['id']}", thumbnail=e["thumbnail"], key=f"enrich:{e['id']}",
            badge=f"{e['gaps']}/{e['total']}"))
    for e in store.album_enrichment_candidates(limit=3):
        enrich.append(ActionItem(
            "enrich", "low", e["title"],
            f"{e['gaps']} of {e['total']} tracks on this saved album are missing genre tags. "
            "Enriching it sharpens recommendations, since the model now leans on these tracks too.",
            "Enrich", f"/album?browse={e['browse_id']}", thumbnail=e["thumbnail"],
            key=f"enrich-album:{e['browse_id']}", badge=f"{e['gaps']}/{e['total']}"))
    items += [i for i in enrich if i.key not in snoozed][:3]

    return [i for i in items if i.key not in snoozed]
