"""Clusters tab: a pan/zoom canvas where you seed a central group (search artists/playlists/songs)
and grow a tree outward. Each node's next ring = library tracks nearest its PINNED-path centroid,
tilted away from the PRUNED set (the per-cluster "negative model"). Saving reuses POST /home/generate
(materialize a Generated playlist + open it on YouTube); the client decides which tracks to send."""
import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from yt_playlist.rec import embed, recommend, genre_map
from yt_playlist.library import executor

RING_SIZE = 6        # tracks added per "grow" — a small ring keeps the canvas legible
ALBUM_CAP = 2        # #14: at most this many tracks from one album per ring (untagged albums uncapped)


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates, now_fn = ctx.store, ctx.templates, ctx.now_fn

    @router.get("/clusters")
    def clusters_page(request: Request):
        from yt_playlist.rec import journeys
        journey_opts = [{"key": k, "label": journeys.JOURNEY_LABELS[k], "hint": journeys.JOURNEY_HINTS[k]}
                        for k in journeys.JOURNEYS]
        return templates.TemplateResponse(request, "clusters.html",
                                          {"have_model": store.rec_vectors_count() > 0,
                                           "journeys": journey_opts})

    @router.get("/clusters/search")
    def clusters_search(q: str = ""):
        """Library autosuggest: vector-backed seeds across artists / playlists / songs."""
        return JSONResponse(store.cluster_search(q, limit=6))

    @router.get("/clusters/genres")
    def clusters_genres():
        """Genre options for the Clusters filter (#29): coarse families AND individual genres
        (sub-genres), each with counts. A selection token may be either kind."""
        return JSONResponse({"families": store.library_genre_families(),
                             "genres": store.library_genres()})

    @router.post("/clusters/expand")
    async def clusters_expand(request: Request):
        """A node's next ring. Body: {pos_keys, neg_keys, exclude, k, allow_families}. pos_keys = the
        pinned path's keys (its centroid); neg_keys = every pruned key (push-away); exclude = keys
        already on the canvas; allow_families = optional genre-family whitelist (#29) — when set, the
        ring is restricted to tracks in those families (untagged tracks dropped). Returns
        {ring: [{key, video_id, title, artist, album, thumbnail, score}]}."""
        body = await request.json()
        pos = body.get("pos_keys") or []
        neg = body.get("neg_keys") or []
        exclude = body.get("exclude") or []
        k = int(body.get("k") or RING_SIZE)
        # tokens may be families or specific genres (#29 / C2b); empty = no restriction
        tokens = body.get("allow_genres") or body.get("allow_families") or []
        allow = store.keys_in_genre_selection(tokens) if tokens else None
        # Over-fetch, then cap per album (#14) so an album can't dominate. The cap is across the WHOLE
        # cluster being built, not just this ring: seed the running count from the tracks already on
        # the canvas (everything in `exclude` that isn't pruned), so once an album hits ALBUM_CAP
        # anywhere in the playlist, no further grow can add more of it.
        nbrs = embed.cluster_expand(store, pos_keys=pos, neg_keys=neg, exclude=exclude,
                                    topn=max(k * 4, k + 12), allow=allow)
        # cap basis = the grown tracks already kept (count_keys), NOT the central seeds — a seed
        # artist's album shouldn't pre-spend the per-album budget. Falls back to non-pruned canvas keys.
        basis = body.get("count_keys")
        if basis is None:
            basis = list(set(exclude) - set(neg))
        album_count = {}
        for m in store.tracks_by_keys(basis).values():
            alb = (m.get("album") or "").strip().lower()
            if alb:
                album_count[alb] = album_count.get(alb, 0) + 1
        cand = [key for key, _ in nbrs]
        meta = store.tracks_by_keys(cand)
        genres = store.track_genres(cand)               # for the client-side genre filter (#29)
        ring = []
        for key, score in nbrs:
            m = meta.get(key)
            if not m or not m.get("video_id"):
                continue
            album = (m.get("album") or "").strip().lower()
            if album:                                    # untagged albums aren't capped
                if album_count.get(album, 0) >= ALBUM_CAP:
                    continue
                album_count[album] = album_count.get(album, 0) + 1
            g = genres.get(key, "")
            ring.append({"key": key, "video_id": m["video_id"], "title": m["title"],
                         "artist": m["artist"], "album": m["album"], "thumbnail": m["thumbnail"],
                         "genre": g, "family": genre_map.family(g), "score": round(score, 4)})
            if len(ring) >= k:
                break
        return JSONResponse({"ring": ring})

    @router.post("/clusters/explain")
    async def clusters_explain(request: Request):
        """Why is this edge here? Body: {key, path_keys}. key = the child track; path_keys = its
        pinned path (central + ancestors). Returns the grounded co-occurrence reasons behind the
        link (shared playlists/album/session, same artist, genre family, decade), falling back to an
        embedding 'bridge' track when nothing is directly shared, plus the taste-space match score.
        Shape: {key, title, artist, headline, reasons:[{kind,text}], score, match_pct}."""
        body = await request.json()
        key = (body.get("key") or "").strip()
        path = [k for k in (body.get("path_keys") or []) if k and k != key]
        if not key:
            return JSONResponse({"key": "", "title": "", "artist": "", "headline": "",
                                 "reasons": [], "score": None, "match_pct": None})
        reasons = list(store.connection_facts(key, path))
        geo = embed.connection_geometry(store, key, path)
        if not reasons and geo["bridge"]:
            bm = store.tracks_by_keys([geo["bridge"]]).get(geo["bridge"])
            if bm:
                reasons.append({"kind": "bridge",
                                "text": f"Linked through “{bm['title']}” by {bm['artist']} — "
                                        f"a track that sits near both."})
        headline = reasons[0]["text"] if reasons else "A close match in your taste space."
        meta = store.tracks_by_keys([key]).get(key, {})
        score = geo["score"]
        return JSONResponse({
            "key": key, "title": meta.get("title", ""), "artist": meta.get("artist", ""),
            "headline": headline, "reasons": reasons,
            "score": round(score, 4) if score is not None else None,
            "match_pct": round(max(0.0, score) * 100) if score is not None else None})

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
            seed_labels = list(json.loads(form.get("seed_labels") or "[]"))
            allow_families = list(json.loads(form.get("allow_families") or "[]"))
        except (ValueError, TypeError):
            keep, central, seed_labels, allow_families = [], [], [], []
        if form.get("include_central"):
            keep += [k for k in central if k not in keep]
        # #15: tag the save with its own tunable 'cluster' recipe, ordered by the chosen DJ journey.
        journey = (form.get("journey") or "auto").strip()
        recipe, order = recommend.cluster_recipe(store, keep, seed_labels, allow_families, journey=journey)
        meta = store.tracks_by_keys(keep)
        tracks = [{"video_id": meta[k]["video_id"], "title": meta[k]["title"],
                   "artist": meta[k]["artist"], "album": meta[k]["album"],
                   "thumbnail": meta[k]["thumbnail"]}
                  for k in order if k in meta and meta[k].get("video_id")]
        identity_id, client = next(iter((ctx.client_provider() or {}).items()), (None, None))
        result = {"name": name}
        if client is None or not tracks:
            result["error"] = "Couldn't create it — connect an account and keep at least one track."
        else:
            try:
                res = await asyncio.to_thread(
                    executor.create_generated_playlist, store, name, tracks, client, now_fn(),
                    identity_id, recipe=recipe)
                result.update(ytm=res["new_ytm"], pid=res["pid"], added=res["added"])
            except Exception:  # noqa: BLE001 - surface a friendly card, log the detail
                ctx.logger.exception("save cluster %r failed", name)
                result["error"] = "YouTube returned an unexpected response."
        resp = templates.TemplateResponse(request, "_partials/generated_result.html", result)
        if not result.get("error"):
            resp.headers["HX-Trigger"] = "cluster-saved"   # tell the canvas to clear (it was created)
        return resp

    return router
