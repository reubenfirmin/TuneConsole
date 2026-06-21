"""Charts tab: top songs/artists by play count, plus per-artist pages."""
from fastapi import APIRouter, HTTPException, Request

from yt_playlist.thumbnails import best_thumb


_WINDOWS = {"all": None, "90d": 90, "30d": 30, "7d": 7}


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
        since = None if days is None else now_fn() - days * 86400.0
        return templates.TemplateResponse(request, "charts.html", {
            "songs": store.top_tracks(100, since=since),
            "artists": store.top_artists(100, since=since),
            "window": win if win in _WINDOWS else "all",
        })

    @router.get("/artist")
    def artist_page(request: Request):
        name = (request.query_params.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=404, detail="no artist specified")
        songs = store.artist_songs(name)
        info = _fetch_artist_info(ctx, name, store.artist_browse_id(name))

        # Section 1 — your collection: albums from your playlist tracks, merged with saved albums.
        coll = {}
        for s in songs:
            key = s["album"] or "Singles / no album"
            d = coll.setdefault(key.lower(), {"album": key, "songs": 0, "plays": 0, "_pls": set(),
                                              "saved": False, "browse": None, "year": None, "thumb": None})
            d["songs"] += 1
            d["plays"] += s["plays"]
            d["thumb"] = d["thumb"] or s["thumbnail"]
            d["_pls"].update(p["ytm"] for p in s["playlists"])

        def _by_artist(a):
            return any(name.lower() == x.strip().lower() for x in (a.get("artist") or "").split(","))

        for a in store.get_saved_albums():
            if not _by_artist(a):
                continue
            key = (a["title"] or "").lower()
            d = coll.get(key)
            if d:
                d["saved"] = True
                d["browse"] = d["browse"] or a["browse"]
                d["year"] = d["year"] or a.get("year")
                d["thumb"] = d["thumb"] or a.get("thumbnail")
            else:
                coll[key] = {"album": a["title"], "songs": 0, "plays": 0, "_pls": set(),
                             "saved": True, "browse": a["browse"], "year": a.get("year"),
                             "thumb": a.get("thumbnail")}
        for d in coll.values():
            d["n_pls"] = len(d.pop("_pls"))
        collection = sorted(coll.values(), key=lambda d: (-d["plays"], (d["album"] or "").lower()))

        # Section 2 — full discography pulled live from YouTube; mark which you've already saved.
        yt_albums = info["albums"] if info and info.get("albums") else []
        saved_ids = store.saved_album_ids()
        for ya in yt_albums:
            ya["saved"] = ya.get("browse_id") in saved_ids
        return templates.TemplateResponse(request, "artist.html", {
            "artist": name, "songs": songs, "collection": collection, "yt_albums": yt_albums,
            "total_plays": sum(s["plays"] for s in songs), "info": info,
        })

    return router
