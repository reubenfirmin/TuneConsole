"""Taste-pinned new-artist discovery.

External sources (Last.fm) supply similarity *edges*; our embedding + play-weighted per-playlist
taste model supply the *judgement*. A candidate new artist is grounded by a match-weighted centroid
of the user's artists it is similar to (the bridge anchors), then scored against the play-weighted
per-playlist taste, so it only surfaces if it fits contexts the user actually plays, and a low-play
playlist can't drag in off-taste artists. Each result explains itself: which of your artists bridged
it, and which of your playlists it fits. Runs in the background worker; Last.fm results cached 14d.
"""
from yt_playlist.core.accounts import catalog_client
from yt_playlist.core.activity import check_stopping, WorkClosing

from yt_playlist.integrations.youtube_music_access import YouTubeMusicAccess
from yt_playlist.rec import rec_params
from yt_playlist.rec.content_projection import ContentProjection as ContentProjection
from yt_playlist.rec.scoring import discovery_album_facet_weights, discovery_facet_weight
from yt_playlist.rec.artist_bridges import anchor_values, rank_candidates as _rank_candidates


def fetch_artist_info(ctx, name, browse_id=None):
    """Best-effort bio + thumbnail + album list for an explicit YouTube artist browseId.
    Returns None without that identity or on failure (no client, network, etc.).

    Lives here (not in the web layer) so outward discovery can reach it without a rec -> web import; the
    artist page in web/routes/charts imports it from here. Module-level so tests can patch it."""
    from yt_playlist.library.discovery_composition import catalog
    return catalog(ctx).artist(name, browse_id)


def _anchors(store, V, idx, top_n=30):
    """[(display_name, weight, unit_vector)]: your artists weighted by play × taste-centrality, so
    the *core* of your taste anchors discovery (not one-off saves)."""
    seeds = store.rec_query.top_played_keys(limit=12)
    refs = store.artists.track_keys(library=True)
    artists = [(store.artists.key(a.get('artist_id') or a['artist']), a['plays'])
               for a in store.charts.top_artists(top_n)]
    return anchor_values(V, idx, seeds, refs, artists)


def repair_artist_images(ctx, artist_ids, now):
    """Repair a small visible cohort in the worker, without a database lock during I/O."""
    bridge = getattr(ctx, 'bridge', None)
    if bridge is not None and not bridge.connected:
        return
    client = YouTubeMusicAccess.for_client(catalog_client(ctx.client_provider()))
    if client is None:
        return
    for artist in ctx.store.discovery.artist_images_due(artist_ids, now):
        check_stopping()
        try:
            thumbnail = client.artist_thumbnail(artist['artist'])
        except WorkClosing:
            raise
        except Exception:
            ctx.logger.debug('Artist image lookup deferred for %r', artist['artist'], exc_info=True)
            ctx.store.discovery.record_artist_image(artist['artist_id'], None, now, retry_after=60)
        else:
            ctx.store.discovery.record_artist_image(artist['artist_id'], thumbnail, now)


def new_artists(ctx, limit=15, max_anchors=30):
    """Taste-pinned new artists: [{artist, score, because[anchors], fits[playlists]}], or []."""
    from yt_playlist.library.discovery_composition import artists
    return artists(ctx).query(limit, max_anchors)


def run_discovery(ctx, now, budget=25) -> dict:
    """One background discovery pass: scan the next batch of interest-ranked artists due for a re-look
    (>5 days since last scan), accumulating their unowned albums into the pool; refresh the taste-
    bridged new-artist pool; prune anything since acquired. Bounded by `budget` so each pass is cheap
    and the pools fill in over many runs rather than re-scanning everything every sync."""
    from yt_playlist.library.discovery_composition import preparation
    return preparation(ctx.store, ctx=ctx).run(now, budget)


def _scan_artist_albums(store, artist, info, owned_albums, saved, genre, now, per, rng, *, artist_id=None) -> None:
    """#52: rotate one artist's pooled albums against a fresh discography scan. Builds the unowned
    catalog, asks discovery_pool which to retain vs add (unshown retained, shown rotated out, slots
    filled by a uniform-random draw across the WHOLE catalog so old albums surface), then applies the
    delete/insert. Caps the artist at `per` albums in the pool."""
    from yt_playlist.library.discovery_composition import preparation
    return preparation(store).scan(artist, info, owned_albums, saved, genre, now, per, rng, artist_id=artist_id)


def enforce_album_bounds(store, keep_artists, per_artist, rng) -> int:
    """#52 (no network): bound the existing album pool. Drop albums from artists not in `keep_artists`
    (the top-N interest set), and trim each kept artist to `per_artist` (retaining unshown first, a
    random-varied subset among ties so the kept sample spans the catalogue). Returns albums removed."""
    from yt_playlist.library.discovery_composition import preparation
    return preparation(store).enforce(keep_artists, per_artist, rng)


def cleanup_discovery_pool(store, rng=None) -> dict:
    """#52 one-time (and per-pass) cleanup with no network: bound the album pool to the top
    discovery_artist_limit artists x discovery_albums_per_artist albums, then prune tracks orphaned by
    the removed albums. Returns {albums_removed, tracks_removed}."""
    from yt_playlist.library.discovery_composition import preparation
    return preparation(store).cleanup(rng)


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
    from yt_playlist.library.discovery_composition import catalog
    return catalog(ctx).radio(now, seeds, per_seed_limit)


def populate_discovered_tracks(ctx, now, budget=4) -> int:
    """#13 Phase 2: fetch the tracks of a few not-yet-populated discovered albums and store them as
    out-of-corpus candidates, tagged with the album's genre, then re-encode the pool's content
    vectors. Bounded by `budget` so each pass is cheap and the pool fills over many runs. Owned
    tracks are skipped. Returns how many candidate tracks were added/updated."""
    from yt_playlist.library.discovery_composition import catalog
    return catalog(ctx).populate(now, budget)


def pick_discovered_albums(store, n, now, recent_frac=0.7, *, facet_leans=None):
    """Surface n albums from the discovered pool: recency-biased (so new releases reliably pop), with
    some older ones mixed in, de-prioritizing what was shown most recently. Selection is read-only;
    the browser records impressions only for visible tiles.

    #18: the FACET overlay applies: albums in a muted genre-family are excluded, and the family
    weight is a secondary bias within the recency buckets (recency stays primary)."""
    albums = store.discovery.get_discovered_albums()
    if not albums:
        return []
    axes = store.genres.feature_axes(a.get('genre') for a in albums)
    families = {axes[a['genre']][0] if a.get('genre') else None for a in albums}
    family_weights = discovery_album_facet_weights(store, families, now, recent=facet_leans)
    return album_values(albums, axes, family_weights, n, recent_frac)


def album_values(albums, axes, family_weights, n, recent_frac=.7):
    wmap, kept = {}, []
    for a in albums:
        family = axes[a['genre']][0] if a.get('genre') else None
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
    axes = store.genres.feature_axes(a.get('genre') for a in arts)
    family_weights = {family: discovery_facet_weight(store, family, now,
        **({'leans': facet_leans} if facet_leans is not None else {}))
        for family in {axes[a['genre']][0] if a.get('genre') else None for a in arts}}
    return artist_values(arts, axes, family_weights, n)


def artist_values(arts, axes, family_weights, n):
    scored = []
    for a in arts:
        family = axes[a['genre']][0] if a.get('genre') else None
        w = family_weights[family]
        if w is None:                                    # muted family: excluded from discovery
            continue
        scored.append((a, (a.get("score") or 0.0) * w))
    scored.sort(key=lambda t: (-t[1], t[0].get("last_shown") or 0.0))
    chosen = [a for a, _ in scored[:n]]
    return chosen
