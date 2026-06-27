"""Taste-pinned new-artist discovery.

External sources (Last.fm) supply similarity *edges*; our embedding + play-weighted per-playlist
taste model supply the *judgement*. A candidate new artist is grounded by a match-weighted centroid
of the user's artists it is similar to (the bridge anchors), then scored against the play-weighted
per-playlist taste, so it only surfaces if it fits contexts the user actually plays, and a low-play
playlist can't drag in off-taste artists. Each result explains itself: which of your artists bridged
it, and which of your playlists it fits. Runs in the background worker; Last.fm results cached 14d.
"""
import random

import numpy as np

from yt_playlist.util import genre_map
from yt_playlist.util.matching import identity_key, normalize
from yt_playlist.util.thumbnails import best_thumb
from yt_playlist.rec import artist_model, embed, recommend, rec_params, discovery_pool
from yt_playlist.providers import lastfm
from yt_playlist.rec.rec_dao import RecDao


def fetch_artist_info(ctx, name, browse_id=None):
    """Best-effort bio + thumbnail + album list from YouTube. Uses the stored artist browseId when we
    have it (accurate); else searches the name. Returns None on any failure (no client, network, etc.).

    Lives here (not in the web layer) so outward discovery can reach it without a rec -> web import; the
    artist page in web/routes/charts imports it from here. Module-level so tests can patch it."""
    try:
        clients = ctx.client_provider() or {}
        client = next(iter(clients.values()), None)
        if client is None:
            return None
        if not browse_id:
            results = client.search(name, filter="artists") or []
            browse_id = results[0].get("browseId") if results else None
        if not browse_id:
            return None
        a = client.get_artist(browse_id)
        albums = []
        for x in (a.get("albums") or {}).get("results") or []:
            albums.append({"title": x.get("title"), "year": x.get("year"),
                           "browse_id": x.get("browseId"),
                           "thumbnail": best_thumb(x.get("thumbnails"))})
        return {"bio": a.get("description"),
                "thumbnail": best_thumb(a.get("thumbnails")),
                "subscribers": a.get("subscribers"),
                "name": a.get("name") or name,
                "browse_id": browse_id,            # the artist's channel, for the "Open in YouTube" link
                "albums": albums}
    except Exception:  # noqa: BLE001 - network/parse/missing-method all degrade to "no info"
        ctx.logger.info("artist info fetch failed for %r (non-fatal)", name)
        return None


class ContentProjection:
    """Learned cold-start grounding (ACARec-flavored): a ridge map from content features to the
    collaborative embedding, fit on the library's own (content, vector) pairs. Lets an *enriched* cold
    candidate get a predicted taste vector to score against the per-playlist model, an alternative to
    the heuristic bridge proxy, kept only if it beats it on recall@k (eval_recs.projection_recall).

    §2: the feature basis is the SHARED audio-aware content space from embed.build_content_model
    (genre family + subgenre + decade + musical key/scale, plus z-scored audio: bpm / energy /
    danceability / moods / loudness / dynamic-complexity), not genre+year alone. Audio is the dominant
    organizing axis in electronic music, so it separates tracks a coarse genre tag cannot (a 172-BPM
    roller from an 88-BPM dub), which is the core lift over the 0.227 genre+year baseline. Reusing the
    same encoder as the content vectors keeps the projection and those vectors in one space. Degrades
    gracefully: a track with only a genre still projects from whatever features it has (never worse
    than the old genre+year grounding). Sharpens as enrichment densifies audio coverage.

    §2c (deferred): for tracks missing audio, Last.fm tags and an artist-similarity signal are the
    intended next fallback features. Artist similarity belongs in the #28 artist-relationship model
    (artist_model.artist_neighbors); see the TODO in fit() for the seam to fold it in once #28 lands.
    """

    def __init__(self, model, W, aidx=None, AV=None):
        self.model = model          # embed content model: {'cat': {tok: col}, 'ncat': int, 'cont': [...]}
        self.W = W                  # (F, dim), F = content_dim + artist_dim
        self.aidx = aidx or {}      # {normalized_artist: row} into the collaborative artist vectors (#28)
        self.AV = AV                # (n_artists, adim) collaborative artist vectors, or None
        self.content_dim = model["ncat"] + len(model["cont"])
        self.adim = AV.shape[1] if AV is not None else 0

    def _features(self, genre, year, audio, artist):
        """Augmented feature row: the content vector (genre/subgenre/decade + audio) concatenated with
        the track artist's collaborative vector (#28 §2c). The artist block lets tracks missing audio
        (or even genre) still ground on the artist-relationship signal; a missing block is zeros."""
        xc = embed.encode_content(self.model, genre, year, audio)
        xc = np.zeros(self.content_dim) if xc is None else xc
        av = np.zeros(self.adim)
        if artist and self.AV is not None:
            r = self.aidx.get(normalize(artist))
            if r is not None:
                av = self.AV[r]
        return np.concatenate([xc, av])

    def predict(self, genre, year=None, audio=None, artist=None):
        """Predicted (un-normalized) taste vector for an enriched candidate. `audio` is the optional
        per-track feature dict; `artist` (display name) folds in the #28 artist signal. Either block
        missing degrades gracefully to the other."""
        return self._features(genre, year, audio, artist) @ self.W

    @classmethod
    def fit(cls, store, lam=1.0):
        keys, V, idx = embed.load_vectors(store)
        if V is None:
            return None
        dao = RecDao(store)
        content, audio = dao.track_content(), dao.track_audio_features()
        model = embed.build_content_model(content, audio)
        artists, AV, aidx = artist_model.load_artist_vectors(store)   # #28 §2c relational feature block
        proj = cls(model, None, aidx, AV)
        rows = []
        for k in idx:                                   # only library tracks that ARE in the embedding
            g, y = content.get(k, (None, None))
            x = proj._features(g, y, audio.get(k), k.rsplit("|", 1)[-1])
            if np.any(x):                               # at least one feature present
                rows.append((k, x))
        if len(rows) < 20:
            return None
        F = proj.content_dim + proj.adim
        X = np.array([x for _, x in rows])
        Y = np.array([V[idx[k]] for k, _ in rows])
        proj.W = np.linalg.solve(X.T @ X + lam * np.eye(F), X.T @ Y)       # ridge closed form: (X'X + λI)^-1 X'Y
        return proj


def _anchors(store, V, idx, top_n=30):
    """[(display_name, weight, unit_vector)]: your artists weighted by play × taste-centrality, so
    the *core* of your taste anchors discovery (not one-off saves)."""
    seeds = store.top_played_keys(limit=12)
    centre = embed._centroid(V, idx, [(seeds, 1.0)]) if seeds else None
    if centre is None:
        centre = V.mean(0)
        centre = centre / (np.linalg.norm(centre) + 1e-9)
    by_artist = {}
    for k in idx:
        by_artist.setdefault(k.rsplit("|", 1)[-1], []).append(idx[k])
    out = []
    for a in store.top_artists(top_n):
        rows = by_artist.get(normalize(a["artist"]))
        if not rows:
            continue
        c = V[rows].mean(0)
        n = np.linalg.norm(c)
        if n == 0:
            continue
        unit = c / n
        weight = a["plays"] * max(float(unit @ centre), 0.0)
        if weight > 0:
            out.append((a["artist"], weight, unit))
    return out


def _artist_thumb(ctx, name):
    """Best-effort artist image from a YTM artist search, for the graphical new-artist cards.
    Cheap (the search result already carries thumbnails; no second get_artist call). None on any miss."""
    try:
        client = next(iter((ctx.client_provider() or {}).values()), None)
        if client is None:
            return None
        results = client.search(name, filter="artists") or []
        return best_thumb(results[0].get("thumbnails")) if results else None
    except Exception:  # noqa: BLE001 - no client / network / parse all degrade to no image
        return None


def new_artists(ctx, limit=15, max_anchors=30):
    """Taste-pinned new artists: [{artist, score, because[anchors], fits[playlists]}], or []."""
    store = ctx.store
    key = lastfm.api_key(store)
    if not key:
        return []
    pt = recommend.playlist_taste(store)
    if not pt:
        return []
    keys, V, idx = embed.load_vectors(store)
    anchors = _anchors(store, V, idx, top_n=max_anchors)
    if not anchors:
        return []
    dao = RecDao(store)
    owned = dao.library_artists()
    now = ctx.now_fn()

    # Refresh the Last.fm similar cache for each anchor: this is the #28 artist model's §C edge source.
    # Key on the normalized name (variants share the cached payload + 14-day TTL); the API call uses
    # the display name. Keep a normalized -> display map so candidates print with their real casing.
    display = {}
    for name, _, _ in anchors:
        nkey = normalize(name)
        cached = dao.cached_similar(nkey, now)
        if cached is None:
            cached = [[n, m] for n, m in lastfm.similar_artists(name, key)]
            dao.cache_similar(nkey, cached, now)
        for n, _ in cached:
            display.setdefault(normalize(n), n)

    # Bridge via the #28 artist model (artist_neighbors blends your co-curation §A + content §B + the
    # Last.fm edges §C cached above), not Last.fm alone. Candidates are normalized vocab names; the
    # taste-fit / because / fits ranking below is kept exactly as before.
    bridges = {}   # candidate -> [(anchor_unit_vec, edge_weight, anchor_name), ...]
    for name, weight, vec in anchors:
        for cand, match in artist_model.artist_neighbors(store, name, topn=50):
            if not cand or cand in owned:
                continue
            # A candidate near several of your anchors accumulates one edge per anchor (append, not
            # clobber); the strength/proxy below then sum over all of them, so multi-anchor fit compounds.
            bridges.setdefault(cand, []).append((vec, weight * float(match), name))

    out = []
    for cand, bl in bridges.items():
        strength = sum(w for _, w, _ in bl)                 # collaborative: Σ anchor_weight × match
        proxy = np.zeros(V.shape[1], dtype=np.float64)
        for v, w, _ in bl:
            proxy += w * v                                  # edge-weighted bridge centroid
        if strength <= 0 or not np.any(proxy):
            continue
        taste, fits = pt.score(proxy)                       # play-weighted per-playlist fit (direction)
        score = taste * strength                            # judged-by-taste × bridge-strength
        if score <= 0:                                      # off-taste (negative cosine). Don't surface
            continue                                        # it as a recommendation with a "fits you" label
        because = [n for _, _, n in sorted(bl, key=lambda x: -x[1])[:3]]
        out.append({"artist": display.get(cand, cand.title()), "score": round(float(score), 4),
                    "because": because, "fits": [t for t, _ in fits]})
    out.sort(key=lambda c: -c["score"])
    out = out[:limit]
    for c in out:                                       # enrich the shown few with an artist image
        c["thumbnail"] = _artist_thumb(ctx, c["artist"])
        c["genre"] = lastfm.artist_genre(c["artist"], key)   # #18: tag for the facet overlay
    return out


def run_discovery(ctx, now, budget=25) -> dict:
    """One background discovery pass: scan the next batch of interest-ranked artists due for a re-look
    (>5 days since last scan), accumulating their unowned albums into the pool; refresh the taste-
    bridged new-artist pool; prune anything since acquired. Bounded by `budget` so each pass is cheap
    and the pools fill in over many runs rather than re-scanning everything every sync."""
    store = ctx.store
    dao = RecDao(store)
    owned_albums, saved = dao.owned_albums(), dao.saved_album_ids()
    artist_genre = dao.artist_genres()                    # #18: tag a new album by its (owned) artist's genre
    # #52: scan only the top-N most-engaged artists, holding a small ROTATING per-artist album sample
    # (old albums surface over time) instead of every album of every artist you have ever touched.
    artist_limit = rec_params.get_param(store, "discovery_artist_limit")
    per_artist = rec_params.get_param(store, "discovery_albums_per_artist")
    rng = random.Random(int(now))                         # varies per pass so the rotation moves over time
    due = store.artists_due_for_scan(now, budget=budget, artist_limit=artist_limit)
    for artist in due:
        try:
            info = fetch_artist_info(ctx, artist)
        except Exception:  # noqa: BLE001 - one bad artist must not abort the pass
            info = None
        _scan_artist_albums(store, artist, info, owned_albums, saved,
                            artist_genre.get(artist), now, per_artist, rng)
        store.mark_scanned(artist, now)
    for a in new_artists(ctx):                            # taste-bridged new artists, accumulated
        store.upsert_discovered_artist(a["artist"], a["score"], a.get("because"), a.get("fits"),
                                       a.get("thumbnail"), now, genre=a.get("genre"))
    store.prune_discovered(owned_albums, saved, dao.library_artists())
    store.prune_discovered_tracks(dao.library_keys(), held_keys=store.generated_track_keys())
    cleanup_discovery_pool(store, rng)                     # #52: bound the pool + prune orphaned tracks
    populate_discovered_tracks(ctx, now)                   # #13 P2: out-of-corpus candidate tracks
    try:
        populate_radio_tracks(ctx, now)                   # #50: radio pulls feed the same cold pool
    except Exception:  # noqa: BLE001 - radio pull is best-effort; never abort the discovery pass
        ctx.logger.info("radio pool population failed (non-fatal)")
    return {"scanned": len(due)}


def _scan_artist_albums(store, artist, info, owned_albums, saved, genre, now, per, rng) -> None:
    """#52: rotate one artist's pooled albums against a fresh discography scan. Builds the unowned
    catalog, asks discovery_pool which to retain vs add (unshown retained, shown rotated out, slots
    filled by a uniform-random draw across the WHOLE catalog so old albums surface), then applies the
    delete/insert. Caps the artist at `per` albums in the pool."""
    catalog = []
    for alb in (info or {}).get("albums") or []:
        bid, title = alb.get("browse_id"), (alb.get("title") or "").strip()
        if not bid or not title or title.lower() in owned_albums or bid in saved:
            continue
        catalog.append({"browse_id": bid, "title": title, "year": alb.get("year"),
                        "thumbnail": alb.get("thumbnail")})
    pooled = store.discovered_albums_for_artist(artist)
    keep, add = discovery_pool.rotate_album_sample(catalog, pooled, per, rng)
    store.delete_discovered_albums([b for b in pooled if b not in keep])
    for alb in add:
        store.upsert_discovered_album(alb["browse_id"], artist, alb["title"], alb["year"],
                                      alb["thumbnail"], now, genre=genre)


def enforce_album_bounds(store, keep_artists, per_artist, rng) -> int:
    """#52 (no network): bound the existing album pool. Drop albums from artists not in `keep_artists`
    (the top-N interest set), and trim each kept artist to `per_artist` (retaining unshown first, a
    random-varied subset among ties so the kept sample spans the catalogue). Returns albums removed."""
    keep = set(keep_artists)
    by_artist: dict = {}
    for a in store.get_discovered_albums():
        by_artist.setdefault(a["artist"], {})[a["browse_id"]] = a.get("offered_count") or 0
    to_delete = []
    for artist, pooled in by_artist.items():
        if artist not in keep:
            to_delete.extend(pooled)                              # whole artist out of the top-N
        elif len(pooled) > per_artist:
            keepset = discovery_pool.choose_album_keep(pooled, per_artist, rng)
            to_delete.extend(b for b in pooled if b not in keepset)
    return store.delete_discovered_albums(to_delete)


def cleanup_discovery_pool(store, rng=None) -> dict:
    """#52 one-time (and per-pass) cleanup with no network: bound the album pool to the top
    discovery_artist_limit artists x discovery_albums_per_artist albums, then prune tracks orphaned by
    the removed albums. Returns {albums_removed, tracks_removed}."""
    rng = rng or random.Random(0)
    limit = rec_params.get_param(store, "discovery_artist_limit")
    per = rec_params.get_param(store, "discovery_albums_per_artist")
    keep_artists = {a["artist"] for a in store.interested_artists(limit=limit)}
    albums_removed = enforce_album_bounds(store, keep_artists, per, rng)
    tracks_removed = store.prune_orphan_discovered_tracks()
    return {"albums_removed": albums_removed, "tracks_removed": tracks_removed}


def gc_discovery(ctx, now) -> dict:
    """#52 daily sweep: remove pool items first shown longer than discovery_gc_days ago and never
    added. A track in a live generated playlist (generated_track_keys) is held until that playlist is
    cleaned up or promoted. Best-effort; returns the per-pool delete counts {albums, artists, tracks}."""
    store = ctx.store
    gc_days = rec_params.get_param(store, "discovery_gc_days")
    held = store.generated_track_keys()
    return store.gc_discovery_pool(now, gc_days, held)


def populate_radio_tracks(ctx, now, seeds=6, per_seed_limit=12) -> int:
    """#50: persist unowned YTM-radio tracks (seeded by your top tracks) into the discovered pool, so
    the cold ranker and Clusters share one growing cold source. Dedups vs owned and the existing pool;
    genre/year/audio are left null for the cold-enrichment worker to fill. Returns how many were added.
    Best-effort: no client/network -> 0. (fresh_songs still produces the radio-order fallback list.)"""
    store = ctx.store
    client = next(iter((ctx.client_provider() or {}).values()), None)
    if client is None:
        return 0
    owned = RecDao(store).library_keys()
    have = {r["identity_key"] for r in store.get_discovered_tracks()}
    added = 0
    for t in store.top_tracks(seeds):
        vid = t.get("video_id")
        if not vid:
            continue
        try:
            radio = client.get_watch_playlist(vid) or {}
        except Exception:  # noqa: BLE001 - network/parse/missing-method -> skip this seed
            continue
        taken = 0
        for r in radio.get("tracks") or []:
            v, title = r.get("videoId"), (r.get("title") or "").strip()
            artist = ((r.get("artists") or [{}])[0] or {}).get("name", "")
            if not v or not title:
                continue
            key = identity_key(title, artist)
            if key in owned or key in have:
                continue
            store.upsert_discovered_track(key, v, title, artist, None,
                                          best_thumb(r.get("thumbnail") or r.get("thumbnails")),
                                          None, None, f"radio:{vid}", now)
            have.add(key)
            added += 1
            taken += 1
            if taken >= per_seed_limit:
                break
    return added


def populate_discovered_tracks(ctx, now, budget=4) -> int:
    """#13 Phase 2: fetch the tracks of a few not-yet-populated discovered albums and store them as
    out-of-corpus candidates, tagged with the album's genre, then re-encode the pool's content
    vectors. Bounded by `budget` so each pass is cheap and the pool fills over many runs. Owned
    tracks are skipped. Returns how many candidate tracks were added/updated."""
    store = ctx.store
    client = next(iter((ctx.client_provider() or {}).values()), None)
    if client is None:
        return 0
    owned = RecDao(store).library_keys()
    have = {t.get("source_browse_id") for t in store.get_discovered_tracks()}
    todo = [a for a in store.get_discovered_albums() if a["browse_id"] not in have][:budget]
    n = 0
    for alb in todo:
        try:
            data = client.get_album(alb["browse_id"])
        except Exception:  # noqa: BLE001 - one bad album must not abort the pass
            continue
        artist = alb.get("artist") or ", ".join(
            x.get("name", "") for x in ((data or {}).get("artists") or []) if x.get("name"))
        for t in (data or {}).get("tracks") or []:
            vid, title = t.get("videoId"), (t.get("title") or "").strip()
            if not vid or not title:
                continue
            key = identity_key(title, artist)
            if key in owned:
                continue
            store.upsert_discovered_track(key, vid, title, artist, alb.get("title"),
                                          alb.get("thumbnail"), alb.get("genre"), alb.get("year"),
                                          alb["browse_id"], now)
            n += 1
    if n:
        embed.build_discovered_content_vectors(store)
    return n


def _discovery_facet_w(store, genre, now):
    """#18 facet overlay for a discovered candidate: its genre-family multiplier, or None to exclude
    (muted family). Untagged → neutral 1.0."""
    fam = genre_map.family(genre) if genre else None
    return recommend.discovery_facet_weight(store, fam, now)


def pick_discovered_albums(store, n, now, recent_frac=0.7):
    """Surface n albums from the discovered pool: recency-biased (so new releases reliably pop), with
    some older ones mixed in, de-prioritizing what was shown most recently. Stamps last_shown.

    #18: the FACET overlay applies: albums in a muted genre-family are excluded, and the family
    weight is a secondary bias within the recency buckets (recency stays primary)."""
    albums = store.get_discovered_albums()
    if not albums:
        return []
    wmap = {}
    kept = []
    for a in albums:
        w = _discovery_facet_w(store, a.get("genre"), now)
        if w is None:                                    # muted family: excluded from discovery
            continue
        wmap[a["browse_id"]] = w
        kept.append(a)
    albums = kept
    if not albums:
        return []
    yrs = [int(a["year"]) for a in albums if (a.get("year") or "").isdigit()]
    ymax = max(yrs) if yrs else 0

    def yr(a):
        return int(a["year"]) if (a.get("year") or "").isdigit() else ymax - 10

    def fresh(a):
        return a.get("last_shown") or 0.0

    cut = ymax - 2                                                            # "recent" = last ~3 years
    # recency primary, then the facet weight (favored families float up among same-year), then freshness
    recent = sorted([a for a in albums if yr(a) >= cut],
                    key=lambda a: (yr(a), wmap[a["browse_id"]], -fresh(a)), reverse=True)
    older = sorted([a for a in albums if yr(a) < cut], key=fresh)             # least-recently-shown first

    seen_art, picked = set(), set()

    def fill(src, k):
        # Take up to k from src, one album per artist first, so a "mixed" + "split" pair of the same
        # release (same artist) won't both surface. Only repeats an artist to reach k if this bucket
        # can't supply k distinct ones, so the recent/older balance below is preserved either way.
        out = []
        for varied in (True, False):
            for a in src:
                if len(out) >= k or a["browse_id"] in picked:
                    continue
                art = (a.get("artist") or "").strip().lower()
                if varied and art and art in seen_art:
                    continue
                if art:
                    seen_art.add(art)
                picked.add(a["browse_id"])
                out.append(a)
        return out

    n_recent = max(1, round(n * recent_frac))
    chosen = fill(recent, n_recent)
    chosen += fill(older, n - len(chosen))
    if len(chosen) < n:                                          # a bucket was thin, top up from the rest
        chosen += fill(recent + older, n - len(chosen))
    chosen = chosen[:n]
    store.mark_shown("album", [a["browse_id"] for a in chosen], now)
    return chosen


def pick_discovered_artists(store, n, now):
    """Surface n new artists from the pool: best taste-score first, de-prioritizing recently-shown.

    #18: the FACET overlay applies: artists in a muted genre-family are excluded, and the family
    weight multiplies the taste score (favored families rise, 'less X lately' gently lowers X)."""
    arts = store.get_discovered_artists()
    if not arts:
        return []
    scored = []
    for a in arts:
        w = _discovery_facet_w(store, a.get("genre"), now)
        if w is None:                                    # muted family: excluded from discovery
            continue
        scored.append((a, (a.get("score") or 0.0) * w))
    scored.sort(key=lambda t: (-t[1], t[0].get("last_shown") or 0.0))
    chosen = [a for a, _ in scored[:n]]
    store.mark_shown("artist", [a["artist"] for a in chosen], now)
    return chosen
