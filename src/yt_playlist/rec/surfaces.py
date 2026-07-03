"""Home recommendation surfaces (cards): Wheelhouse, Comfort, Catalog, Fresh, Rediscover, plus
playlist completion and the Taste-page sample. Each returns a ranked pool; per-card rotation is here."""
import random
import statistics
from collections import Counter
from dataclasses import dataclass

import numpy as np

from yt_playlist.library import analysis
from yt_playlist.util import genre_map
from yt_playlist.util.duration import ago as _ago
from yt_playlist.rec import embed, rec_params
from yt_playlist.rec.rec_dao import RecDao
from yt_playlist.rec.scoring import (_apply_axis_weights, _apply_mood,
                                     content_taste, discovery_facet_weight, playlist_taste)
from yt_playlist.repos.base import GENERATED_GROUP


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
    """A stable-per-epoch random slice of `items`: how a Home list card 'regenerates with a random
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
    per epoch and wrapping once the pool is exhausted, so we cycle back through them rather than
    going empty. Returns up to `size` items (fewer only when the whole pool is smaller)."""
    items = list(items)
    n = len(items)
    if n == 0:
        return []
    start = (epoch * size) % n
    return [items[(start + i) % n] for i in range(min(size, n))]


def _score_candidates(store, pt, keys, V, idx, now):
    """Per-key taste score: per-playlist taste fit, tilted by the transient mood centroid, then scaled
    by the permanent x transient facet weights. The shared scoring core of the Wheelhouse and Catalog
    surfaces. Caller loads pt/keys/V/idx (Catalog needs `keys` afterward) and guards V is not None."""
    scores = _apply_mood(pt.score_all(V), store, now, V, idx)
    return _apply_axis_weights(store, {keys[i]: float(scores[i]) for i in range(len(keys))}, now)


def _weighted_fair_queue(queues, limit, hard, muted):
    """Interleave lane queues by learned weight: each turn serve the lane with the highest
    weight/(taken+1) ratio (default weight 1.0 => round-robin), dedup across lanes, and skip rows that
    are suppressed (`hard`) or by a muted artist. `queues` items are [rows, reason_fn, lane, weight, taken]."""
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


# HOME CARD: "Wheelhouse" ("More in your wheelhouse"). Deeper into what you already love.
# Internal lanes here are 'neighbourhood' + 'deep_cut'. When naming new code/vars, prefer the home
# heading "wheelhouse" over the legacy function name "for_you".
def for_you(store, now, limit=24) -> list[ForYouItem]:
    """Blended local recommendations from your taste model, interleaved and deduped, best-ranked
    first. Returns a deep, ranked pool; per-card rotation (a random epoch-seeded slice) happens at
    the surface, not here: for_you itself carries no anti-staleness state.

    Wheelhouse is your taste/genre model, not play-recency (that's the Comfort Listening card).
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
    # lane's candidate selection AND every lane's final ordering: no separate recency mechanism.
    sims = None
    if store.rec_vectors_count():
        pt = playlist_taste(store)
        keys, V, idx = embed.load_vectors(store)
        if pt and V is not None:
            sims = _score_candidates(store, pt, keys, V, idx, now)

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
    return _weighted_fair_queue(queues, limit, hard, muted)


# HOME CARD: "Comfort" ("Comfort listening"). Most-played favourites that have gone quiet.
def comfort_listening(store, now, limit=24) -> list[ForYouItem]:
    """High-rotation favorites you haven't reached for lately, your comfort listening.

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

    Ranks by *aggregate* track staleness (the median of when each track was last played, with
    never-played tracks counted as cold), so a playlist leads only when most of it has gone unplayed,
    not because one track was heard recently. Then rotates like the other Home cards: pages through
    the coldest `pool` playlists `count` at a time, advancing one page per rotation `epoch` (wrapping),
    so you cycle through only the genuinely-cold tail rather than ever drifting into warmer playlists.
    Highlights `per` tracks from each page as a teaser. Skips system playlists (Liked Music, Episodes
    for Later), Generated proto-playlists, and empties. None of those are a library playlist to revisit.
    """
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
    """Spotlight saved albums you haven't played lately, to nudge a revisit, the album cousin of
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
    """A random *slice* of the tracks that match the current taste model. Backs the Taste page's
    'refresh sample'. Unlike for_you (a deterministic ranking), this draws a deeper matching pool and
    samples from it, so every refresh is a new set even when the knobs are unchanged.
    """
    pool = for_you(store, now, limit=limit * pool_factor)
    if len(pool) <= limit:
        return pool
    return [pool[i] for i in sorted(random.sample(range(len(pool)), limit))]  # keep ranked order in-slice


def _rotation_reason(n) -> str:
    return f"Sits with your favorites in {n} of your playlist{'s' if n != 1 else ''}"


# HOME CARD: "Fresh" ("Fresh songs"). Sourced ONLY from the taste-scored cold pool (cold_candidates,
# materialized by rec_worker._fresh_proposal): every row is a scored, feedback-able out-of-corpus track.
# Radio discovery still feeds that pool via discover.populate_radio_tracks; the old ephemeral radio-dict
# producer (fresh_songs) was removed (#53: pool-only, no unscored radio rows).


def cold_candidates(store, now, limit=None) -> list[ForYouItem]:
    """Rank out-of-corpus (cold) tracks from the discovered pool by taste + the transient tilts (#50).

    Projects each pool track into the collaborative embedding via ContentProjection (so per-playlist
    taste fit and the collaborative mood tilt apply), folds in the audio tilt over the discovered
    content vectors (#45 cold), and scales by the #18 discovery facet overlay (a muted family is
    hard-excluded, consistent with discovered albums/artists). Returns [] when the projection can't be
    fit or the pool is empty. Degrades gracefully: a track missing audio still ranks on genre/era; a
    track with no usable metadata (projection is the zero vector) is dropped, never raising.
    """
    from yt_playlist.rec.discover import ContentProjection   # lazy: avoid the surfaces<->discover cycle
    rows = store.get_discovered_tracks()
    if not rows:
        return []
    proj = ContentProjection.fit(store)
    if proj is None:
        return []
    pt = playlist_taste(store)
    if not pt:
        return []
    keys, vecs, kept = [], [], []
    for r in rows:
        v = proj.predict(r.get("genre"), r.get("year"), r.get("audio"), artist=r.get("artist"))
        if v is None or not np.any(v):                  # no usable features -> not surfaced
            continue
        keys.append(r["identity_key"])
        vecs.append(np.asarray(v, dtype=np.float32))
        kept.append(r)
    if not keys:
        return []
    V = np.vstack(vecs)
    idx = {k: i for i, k in enumerate(keys)}
    # Same scoring pipeline as warm tracks, but the audio tilt scores against the DISCOVERED content
    # vectors (cold tracks' content vectors live there, not in the library content store).
    scores = _apply_mood(pt.score_all(V), store, now, V, idx,
                         content_vecs=embed.load_discovered_content_vectors(store))
    suppressed = store.suppressed_keys("for_you", now)
    muted = store.muted_artists()
    scored = []
    for r in kept:
        k = r["identity_key"]
        if k in suppressed or r.get("artist") in muted:
            continue
        fam = genre_map.family(r.get("genre")) if r.get("genre") else None
        fw = discovery_facet_weight(store, fam, now)
        if fw is None:                                  # muted family: hard-exclude (mirrors #18 discovery)
            continue
        scored.append((float(scores[idx[k]]) * fw, r))
    scored.sort(key=lambda t: -t[0])
    out: list[ForYouItem] = []
    for _, r in (scored[:limit] if limit else scored):
        # The Fresh card label already says these are new, taste-fit tracks, so a generic "fits your
        # taste" per row is pure redundancy: leave it blank. Only the audio-matched tracks get a row
        # note, because "sounds like your recent listening" is genuinely distinguishing info.
        reason = "Sounds like your recent listening" if r.get("audio") else ""
        out.append(ForYouItem(r.get("title") or "", r.get("artist") or "", r.get("album") or "",
                              r.get("video_id"), r.get("thumbnail"), 0, reason,
                              r["identity_key"], lane="cold"))
    return out


def _item_to_fresh_dict(item) -> dict:
    """ForYouItem -> the Fresh-proposal dict. Superset of today's radio dict (adds key/reason/lane for
    feedback parity); the Fresh card template reads video_id/title/artist/thumbnail and ignores extras."""
    return {"video_id": item.video_id, "title": item.title, "artist": item.artist,
            "thumbnail": item.thumbnail, "key": item.key, "reason": item.reason, "lane": item.lane}


CATALOG_CONTENT_FRAC = 0.25   # share of Catalog reserved for content-only (genre/era) rediscovery tracks


def _interleave(primary, secondary, secondary_frac) -> list:
    """Merge two already-ranked lists, drawing ~secondary_frac of slots from `secondary` (in its order)
    and the rest from `primary` (in its order), so the minority list reliably surfaces near the top
    instead of being buried. When one list is exhausted, the rest comes from the other."""
    out, pi, si = [], 0, 0
    for pos in range(len(primary) + len(secondary)):
        want_secondary = (secondary_frac > 0 and si < len(secondary)
                          and (pi >= len(primary) or (pos + 1) * secondary_frac > si))
        if want_secondary:
            out.append(secondary[si]); si += 1
        elif pi < len(primary):
            out.append(primary[pi]); pi += 1
        else:
            out.append(secondary[si]); si += 1
    return out


def _catalog_scores(fit, plays, all_keys=None) -> dict:
    """#86 Catalog kernel: novelty(1/(1+plays)) x a rank base over the taste-fit scores. Keys in
    `all_keys` but absent from `fit` (no scoreable vector) get a below-worst floor so they still
    rank by novelty among themselves instead of zeroing out."""
    base = embed.percentile_scores(fit)
    floor = 0.5 / max(len(fit), 1)
    keys = all_keys if all_keys is not None else fit.keys()
    return {k: (1.0 / (1.0 + plays.get(k, 0))) * base.get(k, floor) for k in keys}


def _content_only_scores(store, plays, now) -> dict:
    """{key: Catalog score} for OWNED tracks that have a content (genre/era) vector but NO co-occurrence
    vector (#38). Scored by content-space taste fit (then the permanent x transient genre/era facet
    overlay, so mutes/steering still apply) x novelty, the same shape as the collaborative Catalog score
    so they interleave. Empty when content vectors or the content-taste model are absent."""
    ct = content_taste(store)
    if not ct:
        return {}
    ckeys, CV, _cidx = embed.load_content_vectors(store)
    if CV is None:
        return {}
    _keys, _V, idx = embed.load_vectors(store)
    owned = set(RecDao(store).library_keys())
    cands = [(i, k) for i, k in enumerate(ckeys) if k in owned and k not in idx]   # owned, content-only
    if not cands:
        return {}
    csims = ct.score_all(CV)
    fit = _apply_axis_weights(store, {k: float(csims[i]) for i, k in cands}, now)
    return _catalog_scores(fit, plays)


# HOME CARD: "Catalog" ("From your catalog"). Own-but-under-played tracks, the edge of your palette.
# This powers the Catalog card, NOT a separate "Explore" surface; the internal lane name 'explore' is
# the source of the naming confusion. The home page dedups this pool against the Wheelhouse pool
# (see web/routes/home.py), so Catalog displays only what Wheelhouse didn't. When naming new
# code/vars, prefer the home heading "catalog" over "explore".
def explore_for_you(store, now, limit=24) -> list[ForYouItem]:
    """Catalog: your OWN under-played tracks, surfaced primarily by LACK OF PLAYS and *weighted*
    (never filtered) by the taste model. So a never-played track that fits your taste tops the card,
    an off-taste never-played track still appears (just lower), and a heavily-played track stays low
    no matter how well it fits. That's the Wheelhouse's job, not Catalog's. Empty until the
    embedding model is built. Distinct from Wheelhouse by signal (plays vs taste), not by a filter.
    """
    plays = store.play_counts()                                  # {key: count}; absent = never played
    pt = playlist_taste(store)
    keys, V, idx = embed.load_vectors(store)
    # Catalog score = novelty(lack of plays, PRIMARY) × taste-fit rank base (a percentile in (0, 1],
    # so it only ever scales, never zeroes: "weighted, not filtered").
    collab = {}
    if pt and V is not None:
        # taste WEIGHT (taste-fit + transient mood + permanent×transient facets): modulates, never filters
        sims = _score_candidates(store, pt, keys, V, idx, now)
        collab = _catalog_scores(sims, plays, all_keys=keys)
    # #38: also rank owned tracks that have a CONTENT (genre/era) vector but no co-occurrence vector,
    # by content-space taste fit. They are invisible to the collaborative path above, yet they are the
    # MOST-forgotten owned tracks (never even reached the co-occurrence model), so Catalog reserves a
    # guaranteed CATALOG_CONTENT_FRAC slice for them rather than letting the ~4.5k co-occurrence tracks
    # fill every slot. The two lists are each ranked internally; interleaving leaves the collaborative
    # order (and the warm path) untouched.
    content = _content_only_scores(store, plays, now)
    if not collab and not content:
        return []
    order = _interleave(sorted(collab, key=lambda k: -collab[k]),
                        sorted(content, key=lambda k: -content[k]), CATALOG_CONTENT_FRAC)
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
        reason = "Never played, sits near your taste" if p == 0 else f"Barely played ({p}×), worth another spin"
        out.append(ForYouItem(m["title"], m["artist"], m["album"], m["video_id"], m["thumbnail"],
                              p, reason, k, "explore"))
        if len(out) >= limit:
            break
    return out


def _take_from_ranked(candidates, limit, per_artist_cap, keep):
    """Walk ranked (key, artist, meta, reason) candidates, applying the keep() suppression/mute filter
    and a per-artist cap (the '529 repeats' guard), building ForYouItems until `limit`. meta is a dict
    with title/artist/album/video_id/thumbnail. Shared by complete_playlist's two candidate sources."""
    out, taken = [], Counter()
    for key, artist, m, reason in candidates:
        if not keep(key, artist) or taken[artist] >= per_artist_cap:
            continue
        taken[artist] += 1
        out.append(ForYouItem(m["title"], m["artist"], m["album"], m["video_id"], m["thumbnail"],
                              0, reason, key))
        if len(out) >= limit:
            break
    return out


def complete_playlist(store, playlist_id, limit=12, now=None) -> list[ForYouItem]:
    """Tracks you own that fit a given playlist but aren't in it yet.

    Uses the taste-embedding model (nearest to the playlist's centroid) once it's built;
    falls back to the artist/co-occurrence heuristic until then.
    """
    members = store.get_playlist_track_keys(playlist_id)
    scope = str(playlist_id)
    suppressed = store.suppressed_keys("suggest", now or 0, scope=scope)
    muted = store.muted_artists()
    member_meta = store.tracks_by_keys(members)
    member_artists = {m["artist"] for m in member_meta.values()}
    # Spread `limit` suggestions across the playlist's distinct artists (>=2 each) so a tightly-
    # clustered artist can't flood an eclectic playlist's completion (the '529 repeats' bug). A
    # single-artist playlist has 1 distinct artist, so the cap is the whole limit. It still gets
    # plenty of that artist.
    distinct = len({m["artist"] for m in member_meta.values()}) or 1
    per_artist_cap = max(2, round(limit / distinct))

    def keep(key, artist):
        return key not in suppressed and artist not in muted

    if store.rec_vectors_count() and members:
        nbrs = embed.centroid_neighbors(store, list(members), topn=limit * 8, exclude=members)
        if nbrs:
            meta = store.tracks_by_keys([k for k, _ in nbrs])
            cands = ((k, m["artist"], m,
                      (f"More from {m['artist']}, already here" if m["artist"] in member_artists
                       else "Matches the sound of this playlist"))
                     for k, _ in nbrs if (m := meta.get(k)))
            out = _take_from_ranked(cands, limit, per_artist_cap, keep)
            if out:                 # if every embedding neighbor was muted/suppressed, don't return
                return out          # an empty list, fall through to the co-occurrence heuristic

    def _cooc_reason(r):
        if r["same_artist"] and r["cooc"]:
            return f"By {r['artist']} (already here), and in {r['cooc']} related playlist(s)"
        if r["same_artist"]:
            return f"More from {r['artist']}, already in this playlist"
        return f"Sits with these tracks in {r['cooc']} of your playlists"

    cooc = ((r["key"], r["artist"], r, _cooc_reason(r))
            for r in store.complete_playlist(playlist_id, limit=limit * 8))
    return _take_from_ranked(cooc, limit, per_artist_cap, keep)


def related_artist_suggestions(store, playlist_id, now, limit=8):
    """#24/#28: tracks by artists RELATED to this playlist's artists (owned + out-of-corpus), for the
    'Complete this playlist' pool. Out-of-corpus pulls are the headline: tracks not yet in the library
    that fit the playlist's artist neighbourhood, which the embedding/co-occurrence completer cannot
    reach. Excludes the playlist's own tracks, suppressed, and muted. Empty until the artist model is
    built. Returned as ForYouItems on the 'related_artist' lane (their reason labels the source)."""
    from yt_playlist.rec import artist_model
    members = store.get_playlist_track_keys(playlist_id)
    seeds = {k.rsplit("|", 1)[-1] for k in members}        # this playlist's (normalized) artists
    if not seeds:
        return []
    member_set = set(members)
    suppressed = store.suppressed_keys("suggest", now or 0, scope=str(playlist_id))
    muted = store.muted_artists()
    out, seen = [], set()
    # Out-of-corpus first: those are the unique value here (the in-library completer already covers
    # owned tracks), so they're not crowded out of the limited pool by owned related-artist tracks.
    cands = sorted(artist_model.artist_track_candidates(store, seeds, topn=limit * 4),
                   key=lambda c: not c.get("out_of_corpus"))
    for c in cands:
        key, artist = c.get("key"), c.get("artist") or ""
        if not key or key in member_set or key in seen or key in suppressed or artist in muted:
            continue
        seen.add(key)
        reason = (f"New: {artist}, related to this playlist's artists" if c.get("out_of_corpus")
                  else f"By {artist}, related to this playlist")
        out.append(ForYouItem(c.get("title") or "", artist, c.get("album") or "", c.get("video_id"),
                              c.get("thumbnail"), 0, reason, key, lane="related_artist"))
        if len(out) >= limit:
            break
    return out


# Canonical home-card names (code vocabulary == product wording). The legacy function names are kept
# as the definitions to avoid churning internal callers; prefer wheelhouse/catalog in new code.
wheelhouse = for_you          # Home card "More in your wheelhouse"
catalog = explore_for_you     # Home card "From your catalog"
