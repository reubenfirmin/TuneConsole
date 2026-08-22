"""Charts tab: top songs/artists by play count, ticker charts (genre/year/album/playlist
listens vs corpus), plus per-artist pages."""
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request

from yt_playlist.rec.discover import fetch_artist_info
from yt_playlist.rec.ticker import candle_geometry, ticker_rows


_WINDOWS = {"all": None, "90d": 90, "30d": 30, "7d": 7}

_TICKER_DIMS = ("genre", "year", "album", "playlist", "artist")
# Only these show the corpus baseline tick + over/under ratio, a "share of your library" baseline
# is intuitive for broad buckets (genre/year/artist) but not for a single album or playlist.
_TICKER_COMPARE = {"genre", "year", "artist"}
_TICKER_TOP = 100     # cap rows per tab (ranked by recent share); genres/years have fewer anyway
_TICKER_BUCKETS = 4   # candle periods, sliced across the actual history span


def _more_albums(discography, collection):
    """Return only discography albums not already represented in the local collection."""
    browse_ids = {a.get("browse") for a in collection if a.get("browse")}
    titles = {(a.get("album") or "").strip().casefold() for a in collection}
    return [a for a in discography
            if a.get("browse_id") not in browse_ids
            and (a.get("title") or "").strip().casefold() not in titles]


def _ticker_periods(earliest, now, window_days=None):
    """DISJOINT, newest-first [since, until) periods. A selected range is the width of each
    period—the 7d chart shows all of the latest seven days and compares it with three earlier 7d
    periods. All-time combines the complete recorded history into one period.
    Returns (periods, close_days, span_days); all-time periods are [] without history.
    """
    if window_days is not None:
        width = window_days * 86400.0
        periods = [(f"w{k}", (now - width * (k + 1), None if k == 0 else now - width * k))
                   for k in range(_TICKER_BUCKETS)]
        return periods, window_days, window_days * _TICKER_BUCKETS
    if earliest is None or earliest >= now:
        return [], 1, 1
    span = now - earliest
    periods = [("w0", (earliest, None))]
    close_days = max(1, round(span / 86400.0))
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
        periods, close_days, span_days = _ticker_periods(earliest, now, days)
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
        browse_id = (request.query_params.get("browse") or "").strip() or None
        if not name:
            raise HTTPException(status_code=404, detail="no artist specified")
        songs = store.artist_songs(name, browse_id)
        info = fetch_artist_info(ctx, name, browse_id or store.artist_browse_id(name))

        # "Saved" is a single source of truth (membership in the saved-album set, keyed by browse_id)
        # so the collection table and the YouTube-discography table below always agree.
        saved_ids = store.saved_album_ids()

        # Section 1, your collection: albums from your playlist tracks, merged with saved albums.
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

        # Section 2, full discography pulled live from YouTube; mark which you've already saved.
        yt_albums = info["albums"] if info and info.get("albums") else []
        yt_albums = _more_albums(yt_albums, collection)
        for ya in yt_albums:
            ya["saved"] = ya.get("browse_id") in saved_ids
        return templates.TemplateResponse(request, "artist.html", {
            "artist": name, "songs": songs, "collection": collection, "yt_albums": yt_albums,
            "total_plays": sum(s["plays"] for s in songs), "info": info,
        })

    return router
