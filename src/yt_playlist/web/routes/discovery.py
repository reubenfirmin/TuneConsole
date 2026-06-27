"""Tools > Discovery Pools (#53): a read-mostly view of the three discovery pools (albums, artists,
tracks) with engagement stats and the projected garbage-collection date (#52), plus an add-to-
collection action that Likes a discovered track (-> Liked Music, so it leaves the pool on next sync)."""
from fastapi import APIRouter, Form, Request
from fastapi.responses import Response

from yt_playlist.rec import rec_params


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates = ctx.store, ctx.templates

    @router.get("/discovery")
    def discovery(request: Request):
        days = rec_params.get_param(store, "discovery_gc_days")
        now = ctx.now_fn()
        return templates.TemplateResponse(request, "discovery.html", {
            "gc_days": days,
            "tracks": store.discovery_track_view(now, days),
            "albums": store.discovery_album_view(now, days),
            "artists": store.discovery_artist_view(now, days),
            "now": now,
        })

    @router.post("/discovery/add")
    def discovery_add(kind: str = Form(...), id: str = Form(...)):
        """Add a discovered item to your collection. Tracks Like into Liked Music (the container-free
        library add); on the next sync the track becomes a library track and the pool prune removes it.
        Returns an empty 200 so the row is swapped out client-side. Best-effort: no client -> 503."""
        client = next(iter((ctx.client_provider() or {}).values()), None)
        if client is None:
            return Response(status_code=503)
        if kind == "track":
            row = store.discovered_tracks_by_keys([id]).get(id)
            vid = row.get("video_id") if row else None
            if vid:
                try:
                    client.rate_song(vid, "LIKE")
                except Exception:  # noqa: BLE001 - a failed like must not 500 the page
                    ctx.logger.warning("discovery add (like) failed for %s", id, exc_info=True)
                    return Response(status_code=502)
        return Response(status_code=200)

    return router
