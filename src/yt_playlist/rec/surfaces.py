"""Home recommendation surfaces (cards): Wheelhouse, Comfort, Catalog, Fresh, Rediscover, plus
playlist completion and the Taste-page sample. Each returns a ranked pool; per-card rotation is here."""
import random
import statistics
from collections import Counter, deque

import numpy as np

from yt_playlist.library import analysis
from yt_playlist.util.duration import ago as _ago
from yt_playlist.util.matching import normalize
from yt_playlist.rec import artist_variety, embedding_vectors as vectors, mix_tuning, rec_params, vector_math
from yt_playlist.rec.discover import ContentProjection
from yt_playlist.rec.scoring import (_apply_axis_weights, _apply_mood, axis_adjusted_scores,
                                     center_known_weights, content_taste, discovery_facet_weight, playlist_taste,
                                     score_with_traces)
from yt_playlist.rec.surface_values import (ForYouItem, neighborhood_keys, wheelhouse_values,
    comfort_values, catalog_order, catalog_eligible, catalog_values, CATALOG_CONTENT_FRAC,
    _weighted_fair_queue, _rotation_reason, _interleave, _catalog_scores)


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


def _score_candidates(store, pt, keys, V, idx, now, include_traces=False):
    """Per-key taste score: per-playlist taste fit, tilted by the transient mood centroid, then scaled
    by the permanent x transient facet weights. The shared scoring core of the Wheelhouse and Catalog
    surfaces. Caller loads pt/keys/V/idx (Catalog needs `keys` afterward) and guards V is not None."""
    from yt_playlist.library.surface_inputs import score_candidates
    return score_candidates(store, pt, keys, V, idx, now, include_traces)


# HOME CARD: "Wheelhouse" ("More in your wheelhouse"). Deeper into what you already love.
# Internal lanes here are 'neighbourhood' + 'deep_cut'. When naming new code/vars, prefer the home
# heading "wheelhouse" over the legacy function name "for_you".
def for_you(store, now, limit=24, include_traces=False) -> list[ForYouItem]:
    """Blended local recommendations from your taste model, interleaved and deduped, best-ranked
    first. Returns a deep, ranked pool; per-card rotation (a random epoch-seeded slice) happens at
    the surface, not here: for_you itself carries no anti-staleness state.

    Wheelhouse is your taste/genre model, not play-recency (that's the Comfort Listening card).
    Sources, strongest-available first:
      - taste neighbourhood: tracks near what you play most, re-ranked by your per-playlist taste
        and genre/era weights (falls back to plain rotation co-occurrence until the model is built)
      - deep cuts: the most-neglected track of each artist you play a lot
    """
    from yt_playlist.library.surface_composition import queries
    return queries(store).wheelhouse(now, limit, include_traces)


# HOME CARD: "Comfort" ("Comfort listening"). Most-played favourites that have gone quiet.
def comfort_listening(store, now, limit=24) -> list[ForYouItem]:
    """High-rotation favorites you haven't reached for lately, your comfort listening.

    Liked or repeatedly played songs qualify only after the configured listening
    rest, including listening in generated queues. Rank quiet favorites by plays,
    honoring the for_you suppression set (dismissed/snoozed/muted).
    """
    from yt_playlist.library.surface_composition import queries
    return queries(store).comfort(now, limit)


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
    recency = store.charts.get_playlist_track_recency()         # {pid: [per-track last-played ts | None, ...]}
    playlists = store.playlists.get_playlists()
    excluded = {p.playlist_scope_id for p in playlists
                if store.playlists.is_quarantined_playlist(p.playlist_scope_id)}
    selected = revisit_playlists_values(playlists, recency, excluded, count, epoch, pool)
    return [{**row, 'tracks': store.charts.playlist_tracks_detail(row['id'])[:per]} for row in
            [dict(id=p.id, ytm=p.provider_playlist_id, title=p.title, track_count=p.track_count,
                  thumbnail=p.thumbnail, last_played=_ago(now-med) if med > 0 else None)
             for p, med in selected]]


def revisit_playlists_values(playlists, recency, excluded, count, epoch, pool):
    candidates = []
    for p in playlists:
        if (p.provider_playlist_id in analysis.SYSTEM_PLAYLIST_IDS
                or p.playlist_scope_id in excluded
                or not p.track_count):
            continue
        # Never-played tracks count as cold: a 0.0 sentinel sorts older than any real timestamp.
        lasts = [t if t is not None else 0.0 for t in (recency.get(p.id) or [0.0])]
        candidates.append((p, statistics.median(lasts)))
    candidates.sort(key=lambda c: c[1])                  # coldest aggregate (oldest median) leads
    cold = candidates[:max(count, pool)]                 # rotate within the coldest tail only
    return rotate_page(cold, count, epoch)


def rediscover_albums(store, now, count=3, epoch=0, pool=9) -> list[dict]:
    """Previously heard saved albums, quiet for at least a month. Unheard saved
    albums belong to discovery, not a claim about remembered favorites."""
    saved = store.collection.get_saved_albums()
    if not saved:
        return []
    recency = store.collection.saved_albums_recency(include_generated=True)
    return revisit_albums_values(saved, recency, now, count, epoch, pool)


def revisit_albums_values(saved, recency, now, count=3, epoch=0, pool=9):
    ranked = sorted((a for a in saved if recency.get(a['browse']) is not None
                     and recency[a['browse']] <= now-30*86400), key=lambda a: recency[a['browse']])
    cold = ranked[:max(count, pool)]                      # rotate within the coldest tail only
    return [{"browse_id": a["browse"], "title": a["title"], "artist": a["artist"],
             "year": a["year"], "thumbnail": a["thumbnail"]}
            for a in rotate_page(cold, count, epoch)]


def rediscover_tracks(store, now, *, keys=None, pool=90):
    """Least-played liked/repeated songs after their required listening rest."""
    rows = store.rec_candidates.revisit_candidates(now, keys=keys, limit=pool)
    blocked = store.rec_model.suppressed_keys('for_you', now, keys=[r['key'] for r in rows])
    muted = store.rec_model.muted_artists()
    return revisit_tracks_values(rows, blocked, muted)


def revisit_tracks_values(rows, blocked, muted):
    return [row for row in rows if row['key'] not in blocked and row['artist'] not in muted]


def revisit_page(items, size, epoch, component):
    """Shuffle a prepared pool once, then advance every visit, even a full page.

    Rendering a fragment again within the same visit does not churn its contents.
    A one-item pool stays honest rather than inventing alternatives.
    """
    items = list(items)
    if not items:
        return []
    random.Random('revisit:' + component).shuffle(items)
    start = epoch % len(items)
    return [items[(start+i) % len(items)] for i in range(min(size, len(items)))]


def taste_sample(store, now, limit=8, pool_factor=8) -> list[ForYouItem]:
    """A random *slice* of the tracks that match the current taste model. Backs the Taste page's
    'refresh sample'. Unlike for_you (a deterministic ranking), this draws a deeper matching pool and
    samples from it, so every refresh is a new set even when the knobs are unchanged.
    """
    pool = for_you(store, now, limit=limit * pool_factor, include_traces=True)
    if len(pool) <= limit:
        return pool
    return [pool[i] for i in sorted(random.sample(range(len(pool)), limit))]  # keep ranked order in-slice


def taste_comparison(store, now, keys=None, limit=24) -> list[dict]:
    """A deterministic production cohort plus traces for requested baseline keys.

    The current top ``limit`` is always included so the UI can identify entrants and exits. Requested
    keys are retained even when they leave that cohort (or become suppressed), allowing an honest
    before/after explanation without treating random live-sample turnover as model movement.
    """
    if not getattr(store, '_read_only', False) and not store._lock.held_by_current_thread:
        with store.read_view() as reader:
            return taste_comparison(reader, now, keys, limit)
    pt = playlist_taste(store, now=now)
    model_keys, V, idx = vectors.load_vectors(store)
    if not pt or V is None:
        return []
    scores, traces = score_with_traces(store, pt, model_keys, V, idx, now)
    served = for_you(store, now, limit=limit, include_traces=True)
    current = {item.key: (rank, item) for rank, item in enumerate(served, 1)}
    wanted = list(dict.fromkeys([*(keys or []), *current]))
    meta = store.rec_query.tracks_by_keys([key for key in wanted if key in traces])
    suppressed = store.rec_model.suppressed_keys("for_you", now)
    muted = store.rec_model.muted_artists()
    weights = store.rec_model.get_weights(
        now=now, revert_halflife_d=rec_params.get_param(store, "weight_revert_halflife_d"))
    genres = store.rec_query.track_genres(wanted)
    genre_axes = store.genres.feature_axes(genres.values())
    return comparison_values(wanted, current, scores, traces, meta, suppressed, muted, weights, genres, genre_axes)


def comparison_values(wanted, current, scores, traces, meta, suppressed, muted, weights, genres, genre_axes):
    rows = []
    for key in wanted:
        if key not in traces or key not in meta:
            continue
        rank, item = current.get(key, (None, None))
        artist = meta[key]["artist"]
        genre = genres.get(key)
        genre_muted = any(weights.get(f"genre:{token}") == 0
                          for token in genre_axes.get(genre, ('', None)) if token)
        reason = ("genre muted" if genre_muted else
                  "dismissed or snoozed" if key in suppressed else
                  "artist muted" if artist in muted else None)
        lane = item.lane if item else ""
        rows.append({"key": key, "title": meta[key]["title"], "artist": artist,
                     "album": meta[key].get("album"), "video_id": meta[key].get("video_id"),
                     "thumbnail": meta[key].get("thumbnail"),
                     "score": scores[key], "trace": traces[key], "rank": rank,
                     "in_cohort": rank is not None, "lane": lane,
                     "lane_weight": weights.get(f"lane:{lane}", 1.0) if lane else None,
                     "eligible": reason is None, "eligibility_reason": reason})
    rows.sort(key=lambda row: (row["rank"] is None, row["rank"] or 0, row["key"]))
    return rows


# HOME CARD: "Fresh" ("Fresh songs"). Draws from the discovery pool. Metadata and artist signals
# improve ranking when available; metadata-bearing discoveries can follow as unscored fallbacks.
# Radio harvesting is separately feature-flagged in discover.populate_radio_tracks.


def _cap_per_artist(scored, cap):
    """Hold an artist to `cap` tracks in a scored [(score, row), ...] pool, best first.

    Discovery walks whole discographies, so the pool is lumpy by construction - measured on a real
    library, 464 pooled tracks with Muse 48 / Shpongle 34 / Underworld 31 of them - and a plain taste
    ranking over that returns one artist's back catalogue: a twelve-track "fresh songs" card that was
    nine Underworld tracks.

    Held-back tracks are appended rather than dropped, so a pool with few artists still fills the
    card instead of coming up short (at the cost of the cap bending once the alternatives run out).
    Every normalized performing identity counts, including collaborators. If no
    identity is available, preserve the legacy normalized whole-credit fallback."""
    per, capped, overflow = Counter(), [], []
    for pair in scored:
        row = pair[1] or {}
        artists = artist_variety.members({**row, 'artist': normalize(row.get('artist') or '')})
        if not any(per[artist] >= cap for artist in artists):
            per.update(artists)
            capped.append(pair)
        else:
            overflow.append(pair)
    return capped + overflow


def cold_candidates(store, now, limit=None) -> list[ForYouItem]:
    """Rank out-of-corpus (cold) tracks from the discovered pool by taste + the transient tilts (#50).

    Projects each pool track into the collaborative embedding via ContentProjection (so per-playlist
    taste fit and the collaborative mood tilt apply), folds in the audio tilt over the discovered
    content vectors (#45 cold), and applies the #18 discovery facet overlay on a positive percentile
    scale, bounded by axis_weight_cap. A muted family is hard-excluded, consistent with discovered
    albums/artists. Tracks with some metadata but no usable vector follow in discovery order.
    Without metadata or a usable artist projection there is no ranking evidence, so the track waits
    for enrichment instead of being surfaced. A missing model never masquerades as a scored result.
    """
    mix_tuning.expire(store, now)
    if not getattr(store, '_read_only', False) and not store._lock.held_by_current_thread:
        with store.read_view() as reader:
            return cold_candidates(reader, now, limit=limit)
    rows = store.discovery.get_discovered_tracks()
    if not rows:
        return []
    identities = store.artists.track_keys(keys=[row['identity_key'] for row in rows])
    rows = [{**row, 'artist_ids': sorted(identities.get(row['identity_key'], ()))} for row in rows]
    proj = ContentProjection.fit(store)
    pt = playlist_taste(store, now=now) if proj is not None else None
    keys, vecs = [], []
    if proj is not None and pt:
        for r in rows:
            v = proj.predict(r.get("genre"), r.get("year"), r.get("audio"), artist=r.get("artist"))
            if v is None or not np.any(v):
                continue
            keys.append(r["identity_key"])
            vecs.append(np.asarray(v, dtype=np.float32))
    scores = {}
    if keys:
        V = np.vstack(vecs)
        idx = {k: i for i, k in enumerate(keys)}
        # Library plays/mood must remain visible even though none of those keys is a candidate.
        # Keep projected discoveries too, so explicit feedback on a cold track still contributes.
        evidence_V, evidence_idx = V, idx
        _, warm_V, warm_idx = vectors.load_vectors(store)
        if warm_V is not None:
            evidence_V = np.vstack((warm_V, V))
            evidence_idx = {k: len(warm_V) + i for k, i in idx.items()}
            evidence_idx.update(warm_idx)  # prefer the actual library vector for overlapping keys
        content_snapshot = store.rec_model.content_generation()
        values = _apply_mood(pt.allocated_scores(V), store, now, V, idx,
                             content_vecs=vectors.load_discovered_content_vectors(store, snapshot=content_snapshot),
                             content_snapshot=content_snapshot,
                             evidence_vecs=(evidence_V, evidence_idx))
        scores = {k: float(values[i]) for i, k in enumerate(keys)}
    suppressed = store.rec_model.suppressed_keys("for_you", now)
    muted = store.rec_model.muted_artists()
    genre_axes = store.genres.feature_axes(r.get('genre') for r in rows)
    family_weights = {family: discovery_facet_weight(store, family, now)
                      for family in {genre_axes[r['genre']][0] if r.get('genre') else None for r in rows}}
    return cold_values(rows, scores, suppressed, muted, genre_axes, family_weights,
                       rec_params.get_param(store, 'axis_weight_cap'),
                       int(rec_params.get_param(store, 'fresh_artist_cap')), limit)


def cold_values(rows, scores, suppressed, muted, genre_axes, family_weights, axis_cap, artist_cap, limit=None):
    candidates, facet_weights = [], {}
    for r in rows:
        k = r["identity_key"]
        if k in suppressed or r.get("artist") in muted:
            continue
        has_metadata = (bool(r.get("genre")) or _as_year(r.get("year")) is not None
                        or any(value not in (None, "") for value in (r.get("audio") or {}).values()))
        if k not in scores and not has_metadata:
            continue
        fam = genre_axes[r['genre']][0] if r.get('genre') else None
        fw = family_weights[fam]
        if fw is None:                                  # muted family: hard-exclude (mirrors #18 discovery)
            continue
        candidates.append(r)
        facet_weights[k] = fw
    # #114/#86: multiplying signed cosine scores reverses boosts below zero. Use the same positive
    # rank base as warm candidates, after exclusions so muted tracks cannot distort percentiles.
    rankable = {r["identity_key"]: scores[r["identity_key"]]
                for r in candidates if r["identity_key"] in scores}
    facet_weights = center_known_weights(
        {k: facet_weights[k] for k in rankable},
        {r["identity_key"]: bool(r.get("genre")) for r in candidates})
    scores = axis_adjusted_scores(rankable, facet_weights,
                                  cap=axis_cap)
    scored = [(scores.get(r["identity_key"]), r) for r in candidates]
    # Keep unknown tracks after ranked tracks, even when a known taste score is negative.
    # Stable sorting preserves discovery order within the fallback instead of inventing a fit score.
    scored.sort(key=lambda t: (t[0] is None, -(t[0] or 0.0)))
    scored = _cap_per_artist(scored, artist_cap)
    out: list[ForYouItem] = []
    for _, r in (scored[:limit] if limit else scored):
        # The Fresh card label already says these are new, taste-fit tracks, so a generic "fits your
        # taste" per row is pure redundancy: leave it blank. Only the audio-matched tracks get a row
        # note, because "sounds like your recent listening" is genuinely distinguishing info.
        reason = "Sounds like your recent listening" if r.get("audio") and r["identity_key"] in scores else ""
        # Carry any saved genre/year onto the item: these tracks are out-of-corpus, so downstream
        # consumers cannot look them up in the library.
        out.append(ForYouItem(r.get("title") or "", r.get("artist") or "", r.get("album") or "",
                              r.get("video_id"), r.get("thumbnail"), 0, reason,
                              r["identity_key"], lane="cold", genre=r.get("genre") or "",
                              year=_as_year(r.get("year")), score=scores.get(r["identity_key"])))
    return out


def _as_year(value):
    """A 4-digit year from a discovered row's loose year field ('1997', '1997-06-16'), or None."""
    text = str(value or "")[:4]
    return int(text) if text.isdigit() else None


def _item_to_fresh_dict(item) -> dict:
    """ForYouItem -> the Fresh-proposal dict. Superset of today's radio dict (adds key/reason/lane for
    feedback parity); the Fresh card template reads video_id/title/artist/thumbnail and ignores extras."""
    return {"video_id": item.video_id, "title": item.title, "artist": item.artist,
            "thumbnail": item.thumbnail, "key": item.key, "reason": item.reason, "lane": item.lane,
            "genre": item.genre, "year": item.year, "score": item.score}


def _content_only_scores(store, plays, now) -> dict:
    """{key: Catalog score} for OWNED tracks that have a content (genre/era) vector but NO co-occurrence
    vector (#38). Scored by content-space taste fit (then the permanent x transient genre/era facet
    overlay, so mutes/steering still apply) x novelty, the same shape as the collaborative Catalog score
    so they interleave. Empty when content vectors or the content-taste model are absent."""
    from yt_playlist.library.surface_inputs import content_scores
    return content_scores(store, plays, now)


# HOME CARD: "Catalog" ("From your catalog"). Own-but-under-played tracks, the edge of your palette.
# This powers the Catalog card, NOT a separate "Explore" surface; the internal lane name 'explore' is
# the source of the naming confusion. Home excludes tracks actually shown in Wheelhouse,
# leaving the rest of this ranked pool available for Catalog. When naming new
# code/vars, prefer the home heading "catalog" over "explore".
def explore_for_you(store, now, limit=24) -> list[ForYouItem]:
    """Catalog: your OWN under-played tracks, surfaced primarily by LACK OF PLAYS and *weighted*
    (never filtered) by the taste model. So a never-played track that fits your taste tops the card,
    an off-taste never-played track still appears (just lower), and a heavily-played track stays low
    no matter how well it fits. That's the Wheelhouse's job, not Catalog's. Owned tracks without
    vectors remain eligible, ranked by novelty after the model-ranked candidates.
    """
    from yt_playlist.library.surface_composition import queries
    return queries(store).catalog(now, limit)


def complete_playlist(store, playlist_id, limit=12, now=None) -> list[ForYouItem]:
    """Genre-compatible tracks by the playlist's own or artist-model-related artists.

    Genre is a hard eligibility boundary, evaluated against each genre region present in the
    playlist (never an averaged playlist centroid). The artist model ranks within that compatible
    pool. Track-level collaborative proximity is deliberately absent: sharing another playlist is
    not evidence that copying more of it improves this one.
    """
    if not getattr(store, '_read_only', False) and not store._lock.held_by_current_thread:
        with store.read_view() as reader:
            return complete_playlist(reader, playlist_id, limit=limit, now=now)
    from yt_playlist.rec import artist_model

    members = store.playlists.get_playlist_track_keys(playlist_id)
    if not members:
        return []
    scope = str(playlist_id)
    suppressed = store.rec_model.suppressed_keys("suggest", now or 0, scope=scope)
    muted = store.rec_model.muted_artists()
    artist_refs = store.artists.track_keys()
    seed_artists = {a for key in members for a in artist_refs.get(key,())}
    dao = store.rec_query
    member_genres = [g for g in dao.track_genres(members).values() if g]
    genre_axes = store.genres.feature_axes(member_genres)
    families = {value[0] for value in genre_axes.values()}
    subgenres = {value[1] for value in genre_axes.values() if value[1]}
    if not seed_artists or not families:
        return []

    related = artist_model.related_artist_scores(
        store, seed_artists, topn=limit * 8, exclude_owned=False)
    candidates = artist_model.artist_track_candidates(
        store, seed_artists, topn=limit * 8, include_out_of_corpus=True,
        scores=related, artist_refs=artist_refs)
    # Same-artist catalog tracks are relevant without asking the model to rediscover the seed itself.
    for c in dao.tracks_by_artists(seed_artists):
        c["artist_score"] = 1.0
        candidates.append(c)

    genre_axes = store.genres.feature_axes(c.get('genre') for c in candidates)
    return completion_values(candidates, members, seed_artists, artist_refs, families, subgenres,
                             genre_axes, suppressed, muted, limit)


def completion_values(candidates, members, seed_artists, artist_refs, families, subgenres,
                      genre_axes, suppressed, muted, limit):
    distinct = len(seed_artists) or 1
    per_artist_cap = max(2, round(limit / distinct))
    ranked = []
    seen = set()
    for c in candidates:
        key, artist, genre = c.get("key"), c.get("artist") or "", c.get("genre") or ""
        family = genre_axes[genre][0] if genre else None
        if (not key or key in members or key in seen or key in suppressed or artist in muted
                or family not in families):
            continue
        seen.add(key)
        sub = genre_axes.get(genre, ('', None))[1]
        exact = int(bool(sub and sub in subgenres))
        score = float(c["artist_score"])
        ranked.append((exact, score, key, artist, family, c,
                       bool(set(artist_refs.get(key,())) & seed_artists)))

    ranked.sort(key=lambda r: (-r[0], -r[1], r[3].lower(), r[2]))
    # Preserve relevance within each artist, then deal one card per artist per round. A plain global
    # sort turns a strong artist with several tracks into a visual wall; round-robin keeps the same
    # candidate set and per-artist ranking while making the grid useful at a glance.
    by_artist = {}
    for row in ranked:
        artist_key = tuple(sorted(artist_refs.get(row[2],())))
        bucket = by_artist.setdefault(artist_key, [])
        if len(bucket) < per_artist_cap:
            bucket.append(row)
    dealt = []
    for depth in range(per_artist_cap):
        dealt.extend(bucket[depth] for bucket in by_artist.values() if depth < len(bucket))

    out = []
    for _exact, _score, key, artist, family, c, same_artist in dealt[:limit]:
        reason = (f"More from {artist} · {family}" if same_artist
                  else f"Related artist · shared {family} genre")
        if c.get("out_of_corpus"):
            reason = "New · " + reason
        out.append(ForYouItem(c.get("title") or "", artist, c.get("album") or "",
                              c.get("video_id"), c.get("thumbnail"), 0, reason, key,
                              lane="related_artist", genre=c.get("genre") or "", score=_score))
    return out
