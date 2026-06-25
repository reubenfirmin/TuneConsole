"""Charts tab: top songs/artists by play count, ticker charts (genre/year/album/playlist
listens vs corpus), plus per-artist pages."""
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request

from yt_playlist.rec.ticker import candle_geometry, ticker_rows
from yt_playlist.util.thumbnails import best_thumb


_WINDOWS = {"all": None, "90d": 90, "30d": 30, "7d": 7}

_TICKER_DIMS = ("genre", "year", "album", "playlist", "artist")
# Only these show the corpus baseline tick + over/under ratio — a "share of your library" baseline
# is intuitive for broad buckets (genre/year/artist) but not for a single album or playlist.
_TICKER_COMPARE = {"genre", "year", "artist"}
_TICKER_TOP = 100     # cap rows per tab (ranked by recent share); genres/years have fewer anyway
_TICKER_BUCKETS = 4   # candle periods, sliced across the actual history span


def _ticker_periods(earliest, now):
    """DISJOINT, newest-first [since, until) periods sized to the ACTUAL history span (earliest ->
    now), so a young library doesn't get mostly-empty fixed 90d/1y windows. Returns
    (periods, close_days, span_days); periods is [] when there's no history.

    Slicing the real span keeps every period populated, so the 'earlier' marker and range whisker
    reflect real data instead of collapsing on null windows.
    """
    if earliest is None or earliest >= now:
        return [], 1, 1
    span = now - earliest
    periods = [(f"w{k}", (now - span * (k + 1) / _TICKER_BUCKETS,
                          None if k == 0 else now - span * k / _TICKER_BUCKETS))
               for k in range(_TICKER_BUCKETS)]
    close_days = max(1, round(span / _TICKER_BUCKETS / 86400.0))
    span_days = max(1, round(span / 86400.0))
    return periods, close_days, span_days


def _ticker_linker(store, dim):
    """Per-dimension `category -> detail-page URL` (or None). Artists/albums/playlists link to
    their pages; genres/years have no detail page."""
    if dim == "artist":
        return lambda cat: f"/artist?name={quote(cat)}"
    if dim == "album":
        browse = store.album_browse_ids()
        return lambda cat: (f"/album?browse={browse[cat]}" if cat in browse else None)
    if dim == "playlist":
        ids = {p.title: p.id for p in store.get_playlists()}
        return lambda cat: (f"/playlist/{ids[cat]}" if cat in ids else None)
    return lambda cat: None


def _build_ticker(store, dim, periods):
    """Assemble one ticker tab: corpus baseline + per-period listen distributions -> ranked rows
    (only categories played at least once in some period, top N by recent share)."""
    corpus = store.corpus_distribution(dim)
    windows = {label: store.listen_distribution(dim, since=lo, until=hi) for label, (lo, hi) in periods}
    data = ticker_rows(corpus, windows)
    rows = [r for r in data["rows"] if r["high"] > 0][:_TICKER_TOP]
    link = _ticker_linker(store, dim)
    for r in rows:
        r["geo"] = candle_geometry(r, data["axis_max"])
        r["link"] = link(r["cat"])
    return {"rows": rows, "axis_max": data["axis_max"]}


def _fetch_artist_info(ctx, name, browse_id=None):
    """Best-effort bio + thumbnail + album list from YouTube. Uses the stored artist browseId when we
    have it (accurate); else searches the name. Returns None on any failure (no client, network, etc.)."""
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
                "browse_id": browse_id,            # the artist's channel — for the "Open in YouTube" link
                "albums": albums}
    except Exception:  # noqa: BLE001 - network/parse/missing-method all degrade to "no info"
        ctx.logger.info("artist info fetch failed for %r (non-fatal)", name)
        return None


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates, now_fn = ctx.store, ctx.templates, ctx.now_fn

    @router.get("/charts")
    def charts_page(request: Request):
        win = request.query_params.get("window", "all")
        days = _WINDOWS.get(win, None)
        now = now_fn()
        since = None if days is None else now - days * 86400.0
        earliest, _latest = store.history_bounds()
        periods, close_days, span_days = _ticker_periods(earliest, now)
        tickers = {dim: _build_ticker(store, dim, periods) for dim in _TICKER_DIMS}
        for dim, t in tickers.items():
            t["close_days"], t["span_days"] = close_days, span_days
            t["compare"] = dim in _TICKER_COMPARE
        return templates.TemplateResponse(request, "charts.html", {
            "songs": store.top_tracks(100, since=since),
            "window": win if win in _WINDOWS else "all",
            "tickers": tickers,
        })

    @router.get("/artist")
    def artist_page(request: Request):
        name = (request.query_params.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=404, detail="no artist specified")
        songs = store.artist_songs(name)
        info = _fetch_artist_info(ctx, name, store.artist_browse_id(name))

        # "Saved" is a single source of truth — membership in the saved-album set, keyed by browse_id —
        # so the collection table and the YouTube-discography table below always agree.
        saved_ids = store.saved_album_ids()

        # Section 1 — your collection: albums from your playlist tracks, merged with saved albums.
        coll = {}
        for s in songs:
            key = s["album"] or "Singles / no album"
            d = coll.setdefault(key.lower(), {"album": key, "songs": 0, "plays": 0, "_pls": set(),
                                              "browse": None, "year": None, "thumb": None})
            d["songs"] += 1
            d["plays"] += s["plays"]
            d["thumb"] = d["thumb"] or s["thumbnail"]
            d["browse"] = d["browse"] or s.get("album_browse")
            d["_pls"].update(p["ytm"] for p in s["playlists"])

        def _by_artist(a):
            return any(name.lower() == x.strip().lower() for x in (a.get("artist") or "").split(","))

        for a in store.get_saved_albums():
            if not _by_artist(a):
                continue
            key = (a["title"] or "").lower()
            d = coll.get(key)
            if d:
                d["browse"] = d["browse"] or a["browse"]
                d["year"] = d["year"] or a.get("year")
                d["thumb"] = d["thumb"] or a.get("thumbnail")
            else:
                coll[key] = {"album": a["title"], "songs": 0, "plays": 0, "_pls": set(),
                             "browse": a["browse"], "year": a.get("year"), "thumb": a.get("thumbnail")}
        for d in coll.values():
            d["n_pls"] = len(d.pop("_pls"))
            d["saved"] = d["browse"] in saved_ids if d["browse"] else False
        collection = sorted(coll.values(), key=lambda d: (-d["plays"], (d["album"] or "").lower()))

        # Section 2 — full discography pulled live from YouTube; mark which you've already saved.
        yt_albums = info["albums"] if info and info.get("albums") else []
        for ya in yt_albums:
            ya["saved"] = ya.get("browse_id") in saved_ids
        return templates.TemplateResponse(request, "artist.html", {
            "artist": name, "songs": songs, "collection": collection, "yt_albums": yt_albums,
            "total_plays": sum(s["plays"] for s in songs), "info": info,
        })

    return router
