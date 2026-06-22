"""Clusters tab: a pan/zoom canvas where you seed a central group (search artists/playlists/songs)
and grow a tree outward. Each node's next ring = library tracks nearest its PINNED-path centroid,
tilted away from the PRUNED set (the per-cluster "negative model"). Saving reuses POST /home/generate
(materialize a Generated playlist + open it on YouTube); the client decides which tracks to send."""
import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from yt_playlist import embed, executor

RING_SIZE = 6        # tracks added per "grow" — a small ring keeps the canvas legible


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates, now_fn = ctx.store, ctx.templates, ctx.now_fn

    @router.get("/clusters")
    def clusters_page(request: Request):
        return templates.TemplateResponse(request, "clusters.html",
                                          {"have_model": store.rec_vectors_count() > 0})

    @router.get("/clusters/search")
    def clusters_search(q: str = ""):
        """Library autosuggest: vector-backed seeds across artists / playlists / songs."""
        return JSONResponse(store.cluster_search(q, limit=6))

    @router.post("/clusters/expand")
    async def clusters_expand(request: Request):
        """A node's next ring. Body: {pos_keys, neg_keys, exclude, k}. pos_keys = the pinned path's
        keys (its centroid); neg_keys = every pruned key (push-away); exclude = keys already on the
        canvas. Returns {ring: [{key, video_id, title, artist, album, thumbnail, score}]}."""
        body = await request.json()
        pos = body.get("pos_keys") or []
        neg = body.get("neg_keys") or []
        exclude = body.get("exclude") or []
        k = int(body.get("k") or RING_SIZE)
        nbrs = embed.cluster_expand(store, pos_keys=pos, neg_keys=neg, exclude=exclude, topn=k)
        meta = store.tracks_by_keys([key for key, _ in nbrs])
        ring = []
        for key, score in nbrs:
            m = meta.get(key)
            if not m or not m.get("video_id"):
                continue
            ring.append({"key": key, "video_id": m["video_id"], "title": m["title"],
                         "artist": m["artist"], "album": m["album"], "thumbnail": m["thumbnail"],
                         "score": round(score, 4)})
        return JSONResponse({"ring": ring})

    @router.post("/clusters/save")
    async def clusters_save(request: Request):
        """Materialize the cluster as a Generated YouTube playlist and open it (same flow as Home's
        proto-save). The canvas posts only identity_keys; we resolve them to saveable track dicts here.
        keep_keys = every non-pruned track on the canvas; central_keys = the seed group's own tracks,
        included only when 'Include central tracks' is ticked."""
        form = await request.form()
        name = (form.get("name") or "").strip()
        try:
            keep = list(json.loads(form.get("keep_keys") or "[]"))
            central = list(json.loads(form.get("central_keys") or "[]"))
        except (ValueError, TypeError):
            keep, central = [], []
        if form.get("include_central"):
            keep += [k for k in central if k not in keep]
        meta = store.tracks_by_keys(keep)
        tracks = [{"video_id": meta[k]["video_id"], "title": meta[k]["title"],
                   "artist": meta[k]["artist"], "album": meta[k]["album"],
                   "thumbnail": meta[k]["thumbnail"]}
                  for k in keep if k in meta and meta[k].get("video_id")]
        identity_id, client = next(iter((ctx.client_provider() or {}).items()), (None, None))
        result = {"name": name}
        if client is None or not tracks:
            result["error"] = "Couldn't create it — connect an account and keep at least one track."
        else:
            try:
                res = await asyncio.to_thread(
                    executor.create_generated_playlist, store, name, tracks, client, now_fn(),
                    identity_id)
                result.update(ytm=res["new_ytm"], pid=res["pid"], added=res["added"])
            except Exception:  # noqa: BLE001 - surface a friendly card, log the detail
                ctx.logger.exception("save cluster %r failed", name)
                result["error"] = "YouTube returned an unexpected response."
        return templates.TemplateResponse(request, "_partials/generated_result.html", result)

    return router
