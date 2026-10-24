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
    return [a for a in discography
            if a.get("browse_id") not in browse_ids]


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
        browse = store.charts.album_browse_ids()
        return lambda cat: (f"/album?browse={browse[cat]}" if cat in browse else None)
    if dim == "playlist":
        ids = {f'@playlist:{p.id}': p.id for p in store.playlists.get_playlists()}
        return lambda cat: (f"/playlist/{ids[cat]}" if cat in ids else None)
    return lambda cat: None


def _build_ticker(store, dim, periods):
    """Assemble one ticker tab: corpus baseline + per-period listen distributions -> ranked rows
    (only categories played at least once in some period, top N by recent share)."""
    corpus = store.charts.corpus_distribution(dim)
    windows = {label: store.charts.listen_distribution(dim, since=lo, until=hi) for label, (lo, hi) in periods}
    data = ticker_rows(corpus, windows)
    rows = [r for r in data["rows"] if r["high"] > 0][:_TICKER_TOP]
    link = _ticker_linker(store, dim)
    thumbnails = store.charts.ticker_thumbnails(dim) if dim in ("artist", "album", "playlist") else {}
    labels = (store.charts.album_labels() if dim == 'album' else
              store.charts.playlist_labels() if dim == 'playlist' else {})
    for r in rows:
        r["geo"] = candle_geometry(r, data["axis_max"])
        r["link"] = link(r["cat"])
        r["thumbnail"] = thumbnails.get(r["cat"])
        r['label'] = labels.get(r['cat'], r['cat'])
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
        earliest, _latest = store.charts.history_bounds()
        periods, close_days, span_days = _ticker_periods(earliest, now, days)
        tickers = {dim: _build_ticker(store, dim, periods) for dim in _TICKER_DIMS}
        for dim, t in tickers.items():
            t["close_days"], t["span_days"] = close_days, span_days
            t["compare"] = dim in _TICKER_COMPARE
        return templates.TemplateResponse(request, "charts.html", {
            "songs": store.charts.top_tracks(100, since=since),
            "window": win if win in _WINDOWS else "all",
            "tickers": tickers,
        })

    @router.get("/artist")
    def artist_page(request: Request):
        name = (request.query_params.get("name") or "").strip()
        browse_id = (request.query_params.get("browse") or "").strip() or None
        artist_id = request.query_params.get('artist_id')
        canonical = store.artists.get_artist('@artist:'+artist_id) if artist_id else None
        if canonical:
            name = canonical['name']
            browse_id = canonical['provider_ids'].get('youtube') or browse_id
        if not name:
            raise HTTPException(status_code=404, detail="no artist specified")
        songs = store.charts.artist_songs(canonical['id'] if canonical else name, browse_id)
        info = fetch_artist_info(ctx, name, browse_id or store.collection.artist_browse_id(name))

        # "Saved" is a single source of truth (membership in the saved-album set, keyed by browse_id)
        # so the collection table and the YouTube-discography table below always agree.
        saved_ids = store.collection.saved_album_ids()

        collection = store.collection.artist_albums(canonical['id'] if canonical else name,browse_id)

        # Section 2, full discography pulled live from YouTube; mark which you've already saved.
        yt_albums = info["albums"] if info and info.get("albums") else []
        yt_albums = _more_albums(yt_albums, collection)
        for ya in yt_albums:
            ya["saved"] = ya.get("browse_id") in saved_ids
        return templates.TemplateResponse(request, "artist.html", {
            "artist": name, "songs": songs, "collection": collection, "yt_albums": yt_albums,
            "saved_owners": store.collection.saved_album_owners(),
            "total_plays": sum(s["plays"] for s in songs), "info": info,
        })

    return router
