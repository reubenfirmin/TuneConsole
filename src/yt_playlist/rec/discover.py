"""Taste-pinned new-artist discovery.

External sources (Last.fm) supply similarity *edges*; our embedding + play-weighted per-playlist
taste model supply the *judgement*. A candidate new artist is grounded by a match-weighted centroid
of the user's artists it is similar to (the bridge anchors), then scored against the play-weighted
per-playlist taste — so it only surfaces if it fits contexts the user actually plays, and a low-play
playlist can't drag in off-taste artists. Each result explains itself: which of your artists bridged
it, and which of your playlists it fits. Runs in the background worker; Last.fm results cached 14d.
"""
import numpy as np

from yt_playlist.rec import embed, genre_map, recommend
from yt_playlist.providers import lastfm
from yt_playlist.rec.rec_dao import RecDao
from yt_playlist.web.routes.charts import _fetch_artist_info   # module-level so it's patchable in tests
from yt_playlist.util.matching import normalize
from yt_playlist.rec.rec_dao import RecDao


class ContentProjection:
    """Learned cold-start grounding (ACARec-flavored): a ridge map from content features
    (genre-family + year-decade, one-hot) to the collaborative embedding, fit on the library's own
    (content, vector) pairs. Lets an *enriched* cold candidate get a predicted taste vector to score
    against the per-playlist model — an alternative to the heuristic bridge proxy, kept only if it
    beats it on recall@k (eval_recs.projection_recall). Sharpens as genre enrichment densifies.
    """

    def __init__(self, feats, W):
        self.feats = feats          # feature name -> column index
        self.W = W                  # (F, dim)

    def _feat_vec(self, genre, year):
        x = np.zeros(len(self.feats))
        fi = self.feats.get(f"fam:{genre_map.family(genre)}")
        if fi is not None:
            x[fi] = 1.0
        if year and str(year)[:4].isdigit():
            di = self.feats.get(f"dec:{int(str(year)[:4]) // 10 * 10}")
            if di is not None:
                x[di] = 1.0
        return x

    def predict(self, genre, year=None):
        """Predicted (un-normalized) taste vector for an enriched candidate."""
        return self._feat_vec(genre, year) @ self.W

    @classmethod
    def fit(cls, store, lam=1.0):
        keys, V, idx = embed.load_vectors(store)
        if V is None:
            return None
        rows = [(k, g, y) for k, (g, y) in RecDao(store).track_content().items() if k in idx]
        if len(rows) < 20:
            return None
        feats = {}
        for _, g, y in rows:
            feats.setdefault(f"fam:{genre_map.family(g)}", len(feats))
            if y:
                feats.setdefault(f"dec:{int(y) // 10 * 10}", len(feats))
        proj = cls(feats, np.zeros((len(feats), V.shape[1])))
        X = np.array([proj._feat_vec(g, y) for _, g, y in rows])
        Y = np.array([V[idx[k]] for k, _, _ in rows])
        W = np.linalg.solve(X.T @ X + lam * np.eye(len(feats)), X.T @ Y)   # ridge, closed form
        return cls(feats, W)


def _anchors(store, V, idx, top_n=30):
    """[(display_name, weight, unit_vector)] — your artists weighted by play × taste-centrality, so
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
    """Best-effort artist image from a YTM artist search — for the graphical new-artist cards.
    Cheap (the search result already carries thumbnails; no second get_artist call). None on any miss."""
    try:
        from yt_playlist.util.thumbnails import best_thumb
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

    def fetch(name):
        # Key the cache on the normalized name (the rest of the feature normalizes too), so
        # casing/autocorrect variants of one artist share the cached payload + 14-day TTL instead
        # of each re-hitting Last.fm. The API call still uses the display name.
        nkey = normalize(name)
        cached = dao.cached_similar(nkey, now)
        if cached is None:
            cached = [[n, m] for n, m in lastfm.similar_artists(name, key)]
            dao.cache_similar(nkey, cached, now)
        return cached

    bridges = {}   # candidate -> [(anchor_unit_vec, edge_weight, anchor_name), ...]
    for name, weight, vec in anchors:
        for cand, match in fetch(name):
            if not cand or normalize(cand) in owned:
                continue
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
        if score <= 0:                                      # off-taste (negative cosine) — don't surface
            continue                                        # it as a recommendation with a "fits you" label
        because = [n for _, _, n in sorted(bl, key=lambda x: -x[1])[:3]]
        out.append({"artist": cand, "score": round(float(score), 4),
                    "because": because, "fits": [t for t, _ in fits]})
    out.sort(key=lambda c: -c["score"])
    out = out[:limit]
    for c in out:                                       # enrich the shown few with an artist image
        c["thumbnail"] = _artist_thumb(ctx, c["artist"])
    return out


def run_discovery(ctx, now, budget=25) -> dict:
    """One background discovery pass: scan the next batch of interest-ranked artists due for a re-look
    (>5 days since last scan), accumulating their unowned albums into the pool; refresh the taste-
    bridged new-artist pool; prune anything since acquired. Bounded by `budget` so each pass is cheap
    and the pools fill in over many runs rather than re-scanning everything every sync."""
    store = ctx.store
    dao = RecDao(store)
    owned_albums, saved = dao.owned_albums(), dao.saved_album_ids()
    due = store.artists_due_for_scan(now, budget=budget)
    for artist in due:
        try:
            info = _fetch_artist_info(ctx, artist)
        except Exception:  # noqa: BLE001 - one bad artist must not abort the pass
            info = None
        for alb in (info or {}).get("albums") or []:
            bid, title = alb.get("browse_id"), (alb.get("title") or "").strip()
            if not bid or not title or title.lower() in owned_albums or bid in saved:
                continue
            store.upsert_discovered_album(bid, artist, title, alb.get("year"), alb.get("thumbnail"), now)
        store.mark_scanned(artist, now)
    for a in new_artists(ctx):                            # taste-bridged new artists, accumulated
        store.upsert_discovered_artist(a["artist"], a["score"], a.get("because"), a.get("fits"),
                                       a.get("thumbnail"), now)
    store.prune_discovered(owned_albums, saved, dao.library_artists())
    return {"scanned": len(due)}


def pick_discovered_albums(store, n, now, recent_frac=0.7):
    """Surface n albums from the discovered pool: recency-biased (so new releases reliably pop), with
    some older ones mixed in, de-prioritizing what was shown most recently. Stamps last_shown."""
    albums = store.get_discovered_albums()
    if not albums:
        return []
    yrs = [int(a["year"]) for a in albums if (a.get("year") or "").isdigit()]
    ymax = max(yrs) if yrs else 0

    def yr(a):
        return int(a["year"]) if (a.get("year") or "").isdigit() else ymax - 10

    def fresh(a):
        return a.get("last_shown") or 0.0

    cut = ymax - 2                                                            # "recent" = last ~3 years
    recent = sorted([a for a in albums if yr(a) >= cut], key=lambda a: (yr(a), -fresh(a)), reverse=True)
    older = sorted([a for a in albums if yr(a) < cut], key=fresh)             # least-recently-shown first

    seen_art, picked = set(), set()

    def fill(src, k):
        # Take up to k from src, one album per artist first — so a "mixed" + "split" pair of the same
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
    if len(chosen) < n:                                          # a bucket was thin — top up from the rest
        chosen += fill(recent + older, n - len(chosen))
    chosen = chosen[:n]
    store.mark_shown("album", [a["browse_id"] for a in chosen], now)
    return chosen


def pick_discovered_artists(store, n, now):
    """Surface n new artists from the pool: best taste-score first, de-prioritizing recently-shown."""
    arts = store.get_discovered_artists()
    if not arts:
        return []
    arts.sort(key=lambda a: (-(a.get("score") or 0.0), a.get("last_shown") or 0.0))
    chosen = arts[:n]
    store.mark_shown("artist", [a["artist"] for a in chosen], now)
    return chosen
