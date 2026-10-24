"""Taste-pinned new-artist discovery.

External sources (Last.fm) supply similarity *edges*; our embedding + play-weighted per-playlist
taste model supply the *judgement*. A candidate new artist is grounded by a match-weighted centroid
of the user's artists it is similar to (the bridge anchors), then scored against the play-weighted
per-playlist taste, so it only surfaces if it fits contexts the user actually plays, and a low-play
playlist can't drag in off-taste artists. Each result explains itself: which of your artists bridged
it, and which of your playlists it fits. Runs in the background worker; Last.fm results cached 14d.
"""
import json
import random
from yt_playlist.core.accounts import catalog_client,main_client
from yt_playlist.core.activity import check_stopping, WorkClosing

import numpy as np

from yt_playlist.util import genre_map
from yt_playlist.util.matching import identity_key, normalize, track_artist
from yt_playlist.util.thumbnails import best_thumb
from yt_playlist.rec import artist_model, content_model, embed, embedding_vectors as vectors, rec_params, discovery_pool
from yt_playlist.rec.scoring import playlist_taste, discovery_facet_weight
from yt_playlist.providers import lastfm


def fetch_artist_info(ctx, name, browse_id=None):
    """Best-effort bio + thumbnail + album list from YouTube. Uses the stored artist browseId when we
    have it (accurate); else searches the name. Returns None on any failure (no client, network, etc.).

    Lives here (not in the web layer) so outward discovery can reach it without a rec -> web import; the
    artist page in web/routes/charts imports it from here. Module-level so tests can patch it."""
    check_stopping()
    try:
        client = catalog_client(ctx.client_provider())
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

    §2: the feature basis is the SHARED audio-aware content space from content_model.build_content_model
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

    def __init__(self, model, W, aidx=None, AV=None, artist_keys=None):
        self.model = model          # embed content model: {'cat': {tok: col}, 'ncat': int, 'cont': [...]}
        self.W = W                  # (F, dim), F = content_dim + artist_dim
        self.aidx = aidx or {}      # {normalized_artist: row} into the collaborative artist vectors (#28)
        self.AV = AV                # (n_artists, adim) collaborative artist vectors, or None
        self.artist_keys = artist_keys or {}
        self.content_dim = model["ncat"] + len(model["cont"])
        self.adim = AV.shape[1] if AV is not None else 0

    def _features(self, genre, year, audio, artist):
        """Augmented feature row: the content vector (genre/subgenre/decade + audio) concatenated with
        the track artist's collaborative vector (#28 §2c). The artist block lets tracks missing audio
        (or even genre) still ground on the artist-relationship signal; a missing block is zeros."""
        xc = content_model.encode_content(self.model, genre, year, audio)
        xc = np.zeros(self.content_dim) if xc is None else xc
        av = np.zeros(self.adim)
        if artist and self.AV is not None:
            names = artist if isinstance(artist,(list,tuple,set)) else [artist]
            rows = [self.aidx.get(self.artist_keys.get(a) or (a if str(a).startswith('@artist:') else normalize(a))) for a in names]
            rows = [r for r in rows if r is not None]
            if rows:
                av = self.AV[rows].mean(0)
        return np.concatenate([xc, av])

    def predict(self, genre, year=None, audio=None, artist=None):
        """Predicted (un-normalized) taste vector for an enriched candidate. `audio` is the optional
        per-track feature dict; `artist` (display name) folds in the #28 artist signal. Either block
        missing degrades gracefully to the other."""
        return self._features(genre, year, audio, artist) @ self.W

    @classmethod
    def fit(cls, store, lam=1.0, *, training_keys=None, include_artist=True):
        """Fit on the requested target identities only, including feature vocabulary/statistics.

        Evaluation disables the precomputed artist block because its fitting history is shared
        with the reference embedding. Production keeps that relational feature.
        """
        keys, V, idx = vectors.load_vectors(store)
        if V is None:
            return None
        dao = store.rec_query
        content, audio = dao.track_content(), dao.track_audio_features()
        if training_keys is not None:
            allowed = set(training_keys)
            idx = {key: row for key, row in idx.items() if key in allowed}
            content = {key: value for key, value in content.items() if key in idx}
            audio = {key: value for key, value in audio.items() if key in idx}
        model = content_model.build_content_model(content, audio)
        artists, AV, aidx = (artist_model.load_artist_vectors(store) if include_artist
                             else ([], None, {}))
        refs = store.artists.track_keys(library=True)
        artist_keys = store.artists.name_keys()
        proj = cls(model, None, aidx, AV,artist_keys)
        rows = []
        for k in idx:                                   # only library tracks that ARE in the embedding
            g, y = content.get(k, (None, None))
            x = proj._features(g, y, audio.get(k), refs.get(k,()))
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
    seeds = store.rec_query.top_played_keys(limit=12)
    centre = embed._centroid(V, idx, [(seeds, 1.0)]) if seeds else None
    if centre is None:
        centre = V.mean(0)
        centre = centre / (np.linalg.norm(centre) + 1e-9)
    by_artist = {}
    refs = store.artists.track_keys(library=True)
    for k in idx:
        for artist in refs.get(k,()):
            by_artist.setdefault(artist, []).append(idx[k])
    out = []
    for a in store.charts.top_artists(top_n):
        rows = by_artist.get(store.artists.key(a.get('artist_id') or a['artist']))
        if not rows:
            continue
        c = V[rows].mean(0)
        n = np.linalg.norm(c)
        if n == 0:
            continue
        unit = c / n
        weight = a["plays"] * max(float(unit @ centre), 0.0)
        if weight > 0:
            out.append((store.artists.key(a.get('artist_id') or a['artist']), weight, unit))
    return out


def _artist_thumb(ctx, name):
    """Best-effort artist image from a YTM artist search, for the graphical new-artist cards.
    Cheap (the search result already carries thumbnails; no second get_artist call). None on any miss."""
    try:
        client = catalog_client(ctx.client_provider())
        if client is None:
            return None
        return _search_artist_thumb(client, name)
    except WorkClosing:
        raise
    except Exception:  # noqa: BLE001 - no client / network / parse all degrade to no image
        return None


def _search_artist_thumb(client, name):
    results = client.search(name, filter="artists") or []
    return best_thumb(results[0].get("thumbnails")) if results else None


def repair_artist_images(ctx, artist_ids, now):
    """Repair a small visible cohort in the worker, without a database lock during I/O."""
    bridge = getattr(ctx, 'bridge', None)
    if bridge is not None and not bridge.connected:
        return
    client = catalog_client(ctx.client_provider())
    if client is None:
        return
    for artist in ctx.store.discovery.artist_images_due(artist_ids, now):
        check_stopping()
        try:
            thumbnail = _search_artist_thumb(client, artist['artist'])
        except WorkClosing:
            raise
        except Exception:
            ctx.logger.debug('Artist image lookup deferred for %r', artist['artist'], exc_info=True)
            ctx.store.discovery.record_artist_image(artist['artist_id'], None, now, retry_after=60)
        else:
            ctx.store.discovery.record_artist_image(artist['artist_id'], thumbnail, now)


def _rank_candidates(bridges, pt, dim) -> list[dict]:
    """#86 Rank bridge candidates by STRENGTH alone (sum of anchor_weight x match; multi-anchor fit
    compounds). The proxy vector is the anchors' own weighted average, so scoring taste FROM it and
    multiplying by strength double-counted the anchors (a candidate near many high-play anchors won
    twice). Taste now only GATES (off-taste proxies are dropped, keeping the 'fits you' label
    honest) and supplies the fits/because explanations."""
    out = []
    for cand, bl in bridges.items():
        strength = sum(w for _, w, _ in bl)
        proxy = np.zeros(dim, dtype=np.float64)
        for v, w, _ in bl:
            proxy += w * v
        if strength <= 0 or not np.any(proxy):
            continue
        taste, fits = pt.score(proxy)
        if taste <= 0:
            continue
        because = [n for _, _, n in sorted(bl, key=lambda x: -x[1])[:3]]
        out.append({"cand": cand, "score": round(float(strength), 4),
                    "because": because, "fits": [t for t, _ in fits]})
    out.sort(key=lambda c: -c["score"])
    return out


def new_artists(ctx, limit=15, max_anchors=30):
    """Taste-pinned new artists: [{artist, score, because[anchors], fits[playlists]}], or []."""
    store = ctx.store
    key = lastfm.api_key(store)
    if not key:
        return []
    pt = playlist_taste(store)
    if not pt:
        return []
    keys, V, idx = vectors.load_vectors(store)
    anchors = _anchors(store, V, idx, top_n=max_anchors)
    if not anchors:
        return []
    dao = store.rec_query
    owned = dao.library_artists()
    now = ctx.now_fn()

    # Refresh the Last.fm similar cache for each anchor: this is the #28 artist model's §C edge source.
    # Cache by canonical anchor reference; the API call uses its display name.
    # Keep a display map for candidates that still lack a provider identity.
    display = {}
    for name, _, _ in anchors:
        check_stopping()
        ref = store.artists.get_artist(name)
        api_name = ref['name'] if ref else name
        nkey = name
        cached = store.artist_similarity.cached_similar(nkey, now)
        if cached is None:
            cached = [[n, m] for n, m in lastfm.similar_artists(api_name, key)]
            store.artist_similarity.cache_similar(nkey, cached, now)
        for n, _ in cached:
            display.setdefault(normalize(n), n)

    # Bridge via the #28 artist model (artist_neighbors blends your co-curation §A + content §B + the
    # Last.fm edges §C cached above), not Last.fm alone. Candidates are normalized vocab names; the
    # taste-fit / because / fits ranking below is kept exactly as before.
    bridges = {}   # candidate -> [(anchor_unit_vec, edge_weight, anchor_name), ...]
    for name, weight, vec in anchors:
        check_stopping()
        ref = store.artists.get_artist(name)
        label = ref['name'] if ref else name
        for cand, match in artist_model.artist_neighbors(store, name, topn=50):
            if not cand or cand in owned:
                continue
            # A candidate near several of your anchors accumulates one edge per anchor (append, not
            # clobber); the strength/proxy below then sum over all of them, so multi-anchor fit compounds.
            bridges.setdefault(cand, []).append((vec, weight * float(match), label))

    ranked = _rank_candidates(bridges, pt, V.shape[1])
    out = []
    for c in ranked[:limit]:
        ref = store.artists.get_artist(c['cand'])
        out.append({'artist':ref['name'] if ref else display.get(c['cand'],c['cand'].title()),
                    'artist_id':ref['id'] if ref else None,'score':c['score'],
                    'because':c['because'],'fits':c['fits']})
    for c in out:                                       # enrich the shown few with an artist image
        check_stopping()
        c["thumbnail"] = _artist_thumb(ctx, c["artist"])
        c["genre"] = lastfm.artist_genre(c["artist"], key)   # #18: tag for the facet overlay
    return out


def run_discovery(ctx, now, budget=25) -> dict:
    """One background discovery pass: scan the next batch of interest-ranked artists due for a re-look
    (>5 days since last scan), accumulating their unowned albums into the pool; refresh the taste-
    bridged new-artist pool; prune anything since acquired. Bounded by `budget` so each pass is cheap
    and the pools fill in over many runs rather than re-scanning everything every sync."""
    store = ctx.store
    dao = store.rec_query
    owned_albums, saved = dao.owned_albums(), store.collection.saved_album_ids()
    artist_genre = dao.artist_genres(canonical=True)
    # #52: scan only the top-N most-engaged artists, holding a small ROTATING per-artist album sample
    # (old albums surface over time) instead of every album of every artist you have ever touched.
    artist_limit = rec_params.get_param(store, "discovery_artist_limit")
    per_artist = rec_params.get_param(store, "discovery_albums_per_artist")
    rng = random.Random(int(now))                         # varies per pass so the rotation moves over time
    due = store.discovery.artists_due_for_scan(now, budget=budget, artist_limit=artist_limit,with_ids=True)
    for ref in due:
        check_stopping()
        artist = ref['artist']
        try:
            info = fetch_artist_info(ctx, artist,ref.get('browse_id'))
        except Exception:  # noqa: BLE001 - one bad artist must not abort the pass
            info = None
        _scan_artist_albums(store, artist, info, owned_albums, saved,
                            artist_genre.get(store.artists.key(ref['artist_id'])), now, per_artist, rng,
                            artist_id=ref['artist_id'])
        store.discovery.mark_scanned(ref['artist_id'], now)
    spotify = store.history.spotify_history_stats()
    source = 'spotify_model' if spotify['events'] or spotify['days'] or spotify['artists'] else 'discovery'
    for a in new_artists(ctx):                            # taste-bridged new artists, accumulated
        store.discovery.upsert_discovered_artist(a["artist"], a["score"], a.get("because"), a.get("fits"),
                                       a.get("thumbnail"), now, genre=a.get("genre"),artist_id=a.get('artist_id'),source=source)
    store.discovery.prune_discovered(owned_albums, saved, dao.library_artists())
    store.discovery.prune_discovered_tracks(dao.library_keys(), held_keys=store.rec_eligibility.generated_track_keys())
    cleanup_discovery_pool(store, rng)                     # #52: bound the pool + prune orphaned tracks
    populate_discovered_tracks(ctx, now)                   # #13 P2: out-of-corpus candidate tracks
    try:
        populate_radio_tracks(ctx, now)                   # #50: radio pulls feed the same cold pool
    except Exception:  # noqa: BLE001 - radio pull is best-effort; never abort the discovery pass
        ctx.logger.info("radio pool population failed (non-fatal)")
    return {"scanned": len(due)}


def _scan_artist_albums(store, artist, info, owned_albums, saved, genre, now, per, rng, *, artist_id=None) -> None:
    """#52: rotate one artist's pooled albums against a fresh discography scan. Builds the unowned
    catalog, asks discovery_pool which to retain vs add (unshown retained, shown rotated out, slots
    filled by a uniform-random draw across the WHOLE catalog so old albums surface), then applies the
    delete/insert. Caps the artist at `per` albums in the pool."""
    catalog = []
    for alb in (info or {}).get("albums") or []:
        bid, title = alb.get("browse_id"), (alb.get("title") or "").strip()
        if not bid or not title or bid in saved or store.collection.album_id(bid) in owned_albums:
            continue
        catalog.append({"browse_id": bid, "title": title, "year": alb.get("year"),
                        "thumbnail": alb.get("thumbnail")})
    pooled = store.discovery.discovered_albums_for_artist(artist_id if artist_id is not None else artist)
    keep, add = discovery_pool.rotate_album_sample(catalog, pooled, per, rng)
    store.discovery.delete_discovered_albums([b for b in pooled if b not in keep])
    for alb in add:
        store.discovery.upsert_discovered_album(alb["browse_id"], artist, alb["title"], alb["year"],
                                      alb["thumbnail"], now, genre=genre,artist_id=artist_id)


def enforce_album_bounds(store, keep_artists, per_artist, rng) -> int:
    """#52 (no network): bound the existing album pool. Drop albums from artists not in `keep_artists`
    (the top-N interest set), and trim each kept artist to `per_artist` (retaining unshown first, a
    random-varied subset among ties so the kept sample spans the catalogue). Returns albums removed."""
    keep = {store.artists.resolve(a) for a in keep_artists}
    by_artist: dict = {}
    for a in store.discovery.get_discovered_albums():
        by_artist.setdefault(a["artist_id"], {})[a["browse_id"]] = a.get("offered_count") or 0
    to_delete = []
    for artist, pooled in by_artist.items():
        if artist not in keep:
            to_delete.extend(pooled)                              # whole artist out of the top-N
        elif len(pooled) > per_artist:
            keepset = discovery_pool.choose_album_keep(pooled, per_artist, rng)
            to_delete.extend(b for b in pooled if b not in keepset)
    return store.discovery.delete_discovered_albums(to_delete)


def cleanup_discovery_pool(store, rng=None) -> dict:
    """#52 one-time (and per-pass) cleanup with no network: bound the album pool to the top
    discovery_artist_limit artists x discovery_albums_per_artist albums, then prune tracks orphaned by
    the removed albums. Returns {albums_removed, tracks_removed}."""
    rng = rng or random.Random(0)
    limit = rec_params.get_param(store, "discovery_artist_limit")
    per = rec_params.get_param(store, "discovery_albums_per_artist")
    keep_artists = {a["artist_id"] for a in store.discovery.interested_artists(limit=limit)}
    albums_removed = enforce_album_bounds(store, keep_artists, per, rng)
    tracks_removed = store.discovery.prune_orphan_discovered_tracks(held_keys=store.rec_eligibility.generated_track_keys())
    return {"albums_removed": albums_removed, "tracks_removed": tracks_removed}


def gc_discovery(ctx, now) -> dict:
    """Daily sweep: expire unsaved suggestions, aging unshown tracks from when they were found.
    A track in a live generated playlist (generated_track_keys) is held until that playlist is
    cleaned up or promoted. Best-effort; returns the per-pool delete counts {albums, artists, tracks}."""
    store = ctx.store
    gc_days = rec_params.get_param(store, "discovery_gc_days")
    held = store.rec_eligibility.generated_track_keys()
    return store.discovery.gc_discovery_pool(now, gc_days, held)


def populate_radio_tracks(ctx, now, seeds=6, per_seed_limit=12) -> int:
    """#50: persist unowned YTM-radio tracks (seeded by your top tracks) into the discovered pool, so
    the cold ranker and Clusters share one growing cold source. Dedups vs owned and the existing pool;
    genre/year/audio are left null for the cold-enrichment worker to fill. Returns how many were added.
    Disabled unless radio_discovery_enabled is explicitly set to "1".
    Best-effort: no client/network -> 0."""
    store = ctx.store
    # Radio is parked. Previewing its UI must not enable periodic catalog harvesting.
    if store.settings.get_setting("radio_discovery_enabled", "0") != "1":
        return 0
    client = main_client(store,ctx.client_provider())
    if client is None:
        return 0
    owned = store.rec_query.library_keys()
    have = {r["identity_key"] for r in store.discovery.get_discovered_tracks()}
    added = 0
    seed_tracks = store.charts.top_tracks(seeds)
    for t in seed_tracks:
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
            artist = track_artist(r)
            if not v or not title:
                continue
            key = store.tracks.identity_key_for_video(v)
            if key in owned:
                continue
            existing = key in have
            key = store.discovery.upsert_discovered_track(identity_key(title, artist), v, title, artist, None,
                                          best_thumb(r.get("thumbnail") or r.get("thumbnails")),
                                          None, None, f"radio:{vid}", now,credits=r.get('artists'))
            have.add(key)
            added += int(not existing)
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
    client = catalog_client(ctx.client_provider())
    if client is None:
        return 0
    owned = store.rec_query.library_keys()
    have = store.discovery.discovered_source_tokens('album')
    todo = [a for a in store.discovery.get_discovered_albums() if a["browse_id"] not in have
            and not store.settings.get_setting("discovery_tracks_fetched:" + a["browse_id"])][:budget]
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
            key = store.tracks.identity_key_for_video(vid)
            if key in owned:
                continue
            credit = track_artist(t) or artist
            store.discovery.upsert_discovered_track(identity_key(title, credit), vid, title, credit, alb.get("title"),
                                          alb.get("thumbnail"), alb.get("genre"), alb.get("year"),
                                          alb["browse_id"], now,credits=t.get('artists') or data.get('artists'))
            n += 1
        if (data or {}).get("tracks"):
            store.settings.set_setting("discovery_tracks_fetched:" + alb["browse_id"], "1")
    if n:
        vectors.build_discovered_content_vectors(store)
    return n


def _discovery_facet_w(store, genre, now, *, leans=None):
    """#18 facet overlay for a discovered candidate: its genre-family multiplier, or None to exclude
    (muted family). Untagged → neutral 1.0."""
    fam = genre_map.family(genre) if genre else None
    return discovery_facet_weight(store, fam, now, **({'leans': leans} if leans is not None else {}))


def pick_discovered_albums(store, n, now, recent_frac=0.7, *, facet_leans=None):
    """Surface n albums from the discovered pool: recency-biased (so new releases reliably pop), with
    some older ones mixed in, de-prioritizing what was shown most recently. Selection is read-only;
    the browser records impressions only for visible tiles.

    #18: the FACET overlay applies: albums in a muted genre-family are excluded, and the family
    weight is a secondary bias within the recency buckets (recency stays primary)."""
    albums = store.discovery.get_discovered_albums()
    if not albums:
        return []
    wmap, family_weights = {}, {}
    kept = []
    for a in albums:
        family = genre_map.family(a["genre"]) if a.get("genre") else None
        if family not in family_weights:
            family_weights[family] = _discovery_facet_w(store, a.get("genre"), now, leans=facet_leans)
        w = family_weights[family]
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
    return chosen


def pick_discovered_artists(store, n, now, *, facet_leans=None):
    """Surface n new artists from the pool: best taste-score first, de-prioritizing recently-shown.

    #18: the FACET overlay applies: artists in a muted genre-family are excluded, and the family
    weight multiplies the taste score (favored families rise, 'less X lately' gently lowers X)."""
    arts = store.discovery.get_discovered_artists()
    if not arts:
        return []
    scored = []
    family_weights = {}
    for a in arts:
        family = genre_map.family(a["genre"]) if a.get("genre") else None
        if family not in family_weights:
            family_weights[family] = _discovery_facet_w(store, a.get("genre"), now, leans=facet_leans)
        w = family_weights[family]
        if w is None:                                    # muted family: excluded from discovery
            continue
        scored.append((a, (a.get("score") or 0.0) * w))
    scored.sort(key=lambda t: (-t[1], t[0].get("last_shown") or 0.0))
    chosen = [a for a, _ in scored[:n]]
    return chosen
