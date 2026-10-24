"""Clusters tab: a pan/zoom canvas where you seed a central group (search artists/playlists/songs)
and grow a tree outward. Each node's next ring = library tracks nearest its PINNED-path centroid,
tilted away from the PRUNED set (the per-cluster "negative model"). Saving reuses POST /home/generate
(materialize a Generated playlist + open it on YouTube); the client decides which tracks to send."""
from yt_playlist.core.activity import activity_for
from yt_playlist.core.accounts import client_for
import json
import random

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from yt_playlist.util import genre_map
from yt_playlist.rec import embed, measurement, recipes
from yt_playlist.rec.cluster_explain import connection_reasons
from yt_playlist.library import playlist_generated, playlist_navigation
from yt_playlist.library.playlist_writes import PlaylistAddUncertain
from yt_playlist.web.cluster_scans import ClusterScans
from yt_playlist.web.cluster_projection import search_results
from yt_playlist.core.activity import check_stopping

RING_SIZE = 6        # tracks added per "grow", a small ring keeps the canvas legible
ALBUM_CAP = 2        # #14: at most this many tracks from one album per ring (untagged albums uncapped)


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates, now_fn = ctx.store, ctx.templates, ctx.now_fn
    scans = ClusterScans(ctx)

    @router.get("/clusters")
    def clusters_page(request: Request):
        from yt_playlist.rec import journeys
        journey_opts = [{"key": k, "label": journeys.JOURNEY_LABELS[k], "hint": journeys.JOURNEY_HINTS[k]}
                        for k in journeys.JOURNEYS]
        return templates.TemplateResponse(request, "clusters.html",
                                          {"have_model": store.rec_model.rec_vectors_count() > 0,
                                           "journeys": journey_opts})

    @router.get("/clusters/search")
    def clusters_search(q: str = ""):
        """Library autosuggest: vector-backed seeds across artists / playlists / songs."""
        return JSONResponse(search_results(store.cluster_evidence.seed_matches(q, limit=6)))

    @router.get("/clusters/state/{ytm}")
    def clusters_state(ytm: str):
        """The saved canvas behind a generated cluster playlist (#48), so it can be reopened and
        regrown a different way. Returns the stored blob verbatim, or 404 when there is none."""
        state = store.playlist_recipes.get_cluster_canvas(ytm)
        if not state:
            return JSONResponse({"error": "no canvas"}, status_code=404)
        return Response(content=state, media_type="application/json")

    @router.get("/clusters/genres")
    def clusters_genres():
        """Genre options for the Clusters filter (#29): coarse families AND individual genres
        (sub-genres), each with counts. A selection token may be either kind."""
        return JSONResponse({"families": store.rec_query.library_genre_families(),
                             "genres": store.rec_query.library_genres()})

    def _expand(store, body):
        """A node's next ring. Body: {pos_keys, neg_keys, exclude, k, allow_families}. pos_keys = the
        pinned path's keys (its centroid); neg_keys = every pruned key (push-away); exclude = keys
        already on the canvas; allow_families = optional genre-family whitelist (#29), when set, the
        ring is restricted to tracks in those families (untagged tracks dropped). Returns
        {ring: [{key, video_id, title, artist, album, thumbnail, score}]}."""
        pos = body.get("pos_keys") or []
        neg = body.get("neg_keys") or []
        exclude = body.get("exclude") or []
        k = int(body.get("k") or RING_SIZE)
        include_new = bool(body.get("include_new"))      # Phase 2: reach out-of-corpus discovered tracks
        # tokens may be families or specific genres (#29 / C2b); empty = no restriction
        tokens = body.get("allow_genres") or body.get("allow_families") or []
        allow = store.rec_query.keys_in_genre_selection(tokens) if tokens else None
        seeds = body.get("central_seeds") or []
        groups = [s["keys"] for s in seeds if s.get("keys")]
        artists = [s for s in seeds if s.get("kind") == "artist" and s.get("keys")]
        artist_keys = [set(s["keys"]) for s in artists]
        song_keys = {key for s in seeds if s.get("kind") == "song" for key in s.get("keys", [])}
        preferred = {key for s in artists for key in s["keys"]} - song_keys
        # Over-fetch, then cap per album (#14) so an album can't dominate. The cap is across the WHOLE
        # cluster being built, not just this ring: seed the running count from the tracks already on
        # the canvas (everything in `exclude` that isn't pruned), so once an album hits ALBUM_CAP
        # anywhere in the playlist, no further grow can add more of it.
        nbrs = embed.cluster_expand(store, pos_keys=pos, neg_keys=neg, exclude=exclude,
                                    topn=len(preferred) + max(k * 4, k + 12), allow=allow,
                                    include_new=include_new, seed_groups=groups,
                                    candidate_seed_keys=preferred, artist_groups=artist_keys)
        # cap basis = the grown tracks already kept (count_keys), NOT the central seeds. A seed
        # artist's album shouldn't pre-spend the per-album budget. Falls back to non-pruned canvas keys.
        basis = body.get("count_keys")
        if basis is None:
            basis = list(set(exclude) - set(neg))
        album_count = {}
        for m in store.rec_query.tracks_by_keys(basis).values():
            alb = (m.get("album") or "").strip().lower()
            if alb:
                album_count[alb] = album_count.get(alb, 0) + 1
        cand = [key for key, _ in nbrs]
        meta = store.rec_query.tracks_by_keys(cand)
        dmeta = store.discovery.discovered_tracks_by_keys(cand) if include_new else {}   # out-of-corpus tracks
        genres = store.rec_query.track_genres(cand)               # for the client-side genre filter (#29)
        # Seeded variation among near matches, stable when a saved canvas is reopened. Balance the
        # artists while selecting (after album eligibility), so a large catalog cannot crowd out a
        # second artist. Related tracks fill any remaining places when seed songs run out.
        rng = random.Random(str(body.get("random_seed", 0)))
        variation = 0.02 if len(artists) > 1 else 0.12
        rank = {key: score + rng.uniform(-variation, variation) for key, score in sorted(nbrs)} if seeds else dict(nbrs)
        memberships = {key: [i for i, keys in enumerate(artist_keys) if key in keys]
                       for key in cand}
        artist_counts = [0] * len(artists)
        candidates = {key: score for key, score in nbrs
                      if (meta.get(key) or dmeta.get(key) or {}).get("video_id")}
        ring = []
        while candidates and len(ring) < k:
            check_stopping()
            eligible = [key for key in candidates
                        if not (album := ((meta.get(key) or dmeta[key]).get("album") or "").strip().lower())
                        or album_count.get(album, 0) < ALBUM_CAP]
            if not eligible:
                break
            def priority(key):
                member = memberships[key] if key in preferred else []
                return (bool(member), -min((artist_counts[i] for i in member), default=0), rank[key])
            key = max(eligible, key=priority)
            score = candidates.pop(key)
            new = key not in meta and key in dmeta       # out-of-corpus candidate
            m = meta.get(key) or dmeta.get(key)
            album = (m.get("album") or "").strip().lower()
            if album:
                album_count[album] = album_count.get(album, 0) + 1
            for i in memberships[key]:
                artist_counts[i] += 1
            g = (m.get("genre") if new else genres.get(key, "")) or ""
            ring.append({"key": key, "video_id": m["video_id"], "title": m["title"],
                         "artist": m["artist"], "album": m["album"], "thumbnail": m["thumbnail"],
                         "genre": g, "family": genre_map.family(g), "score": round(score, 4),
                         "out_of_corpus": new})
        return {"ring": ring}, [track["key"] for track in ring if track["key"] in dmeta]

    def _record_expand(payload, offered, body):
        if offered:
            store.discovery.mark_offered("track", offered, now_fn())
        measurement.record_recommendations(store, "clusters", payload['ring'], now_fn(), lane="ring",
                                           ranker="cluster-expand",
                                           provenance={"positive_seeds": body.get('pos_keys') or [],
                                                       "negative_seeds": body.get('neg_keys') or [],
                                                       "genre_selection": body.get('allow_genres') or
                                                       body.get('allow_families') or []})

    @router.post("/clusters/expand")
    async def clusters_expand(request: Request):
        body = await request.json()
        return JSONResponse(await scans.run(_expand, body, _record_expand))

    def _explain(store, body):
        """Why is this edge here? Body: {key, path_keys}. key = the child track; path_keys = its
        pinned path (central + ancestors). Returns the grounded co-occurrence reasons behind the
        link (shared playlists/album/session, same artist, genre family, decade), falling back to an
        embedding 'bridge' track when nothing is directly shared, plus the taste-space match score.
        Shape: {key, title, artist, headline, reasons:[{kind,text}], score, match_pct}."""
        key = (body.get("key") or "").strip()
        path = [k for k in (body.get("path_keys") or []) if k and k != key]
        if not key:
            return {"key": "", "title": "", "artist": "", "headline": "",
                    "reasons": [], "score": None, "match_pct": None}, None
        reasons = connection_reasons(store.cluster_evidence.connection(key, path))
        geo = embed.connection_geometry(store, key, path)
        if not reasons and geo["bridge"]:
            bm = store.rec_query.tracks_by_keys([geo["bridge"]]).get(geo["bridge"])
            if bm:
                reasons.append({"kind": "bridge",
                                "text": f"Linked through “{bm['title']}” by {bm['artist']}, "
                                        f"a song similar to both."})
        headline = reasons[0]["text"] if reasons else "Similar to the music you chose."
        meta = store.rec_query.tracks_by_keys([key]).get(key, {})
        score = geo["score"]
        return {
            "key": key, "title": meta.get("title", ""), "artist": meta.get("artist", ""),
            "headline": headline, "reasons": reasons,
            "score": round(score, 4) if score is not None else None,
            "match_pct": round(max(0.0, score) * 100) if score is not None else None}, None

    @router.post("/clusters/explain")
    async def clusters_explain(request: Request):
        body = await request.json()
        return JSONResponse(await scans.run(_explain, body))

    @router.post("/clusters/save")
    async def clusters_save(request: Request):
        """Materialize the cluster as a Generated YouTube playlist and open it (same flow as Home's
        proto-save). The canvas posts only identity_keys; we resolve them to saveable track dicts here.
        keep_keys = every non-pruned track on the canvas; central_keys = the seed group's own tracks,
        always folded in when present."""
        form = await request.form()
        return await activity_for(ctx).to_thread(_save_cluster, request, form)

    def _save_cluster(request, form):
        # Metadata, greedy ordering and save bookkeeping can all wait on substantial work.
        # Track the whole operation outside the event loop, including any remote mutation.
        name = (form.get("name") or "").strip()
        try:
            keep = list(json.loads(form.get("keep_keys") or "[]"))
            central = list(json.loads(form.get("central_keys") or "[]"))
            seed_labels = list(json.loads(form.get("seed_labels") or "[]"))
            allow_families = list(json.loads(form.get("allow_families") or "[]"))
        except (ValueError, TypeError):
            keep, central, seed_labels, allow_families = [], [], [], []
        keep += [k for k in central if k not in keep]   # central seed tracks are always folded in
        # #15: tag the save with its own tunable 'cluster' recipe, ordered by the chosen DJ journey.
        journey = (form.get("journey") or "auto").strip()
        recipe, order = recipes.cluster_recipe(store, keep, seed_labels, allow_families, journey=journey)
        meta = store.rec_query.tracks_by_keys(keep)
        meta = {**store.discovery.discovered_tracks_by_keys(keep), **meta}   # resolve out-of-corpus keys too (library wins)
        tracks = [{"video_id": meta[k]["video_id"], "title": meta[k]["title"],
                   "artist": meta[k]["artist"], "album": meta[k]["album"],
                   "thumbnail": meta[k]["thumbnail"]}
                  for k in order if k in meta and meta[k].get("video_id")]
        account_error = None
        try:
            identity_id,client = client_for(store,ctx.client_provider())
        except ValueError as exc:
            identity_id,client,account_error = None,None,str(exc)
        result = {"name": name}
        if client is None or not tracks:
            result["error"] = account_error or "Keep at least one track to create a playlist."
        else:
            try:
                res = playlist_generated.create_generated_playlist(store, name, tracks, client, now_fn(),
                                                         identity_id, recipe=recipe)
                result.update(ytm=res["new_ytm"], pid=res["pid"], added=res["added"])
                if res["added"]:
                    measurement.record_action(store, "save", now_fn(), playlist_id=res["pid"],
                                              event_id=f"generate:{res['new_ytm']}", generated=True)
                # Play in the already-open YouTube Music tab (background), same as /home/generate:
                # wait until YouTube confirms the new playlist is loadable before switching, on a
                # background thread so this response is not held.
                bridge = getattr(ctx, "bridge", None)
                if bridge is not None and getattr(bridge, "connected", False):
                    watch_url = f"https://music.youtube.com/watch?list={res['new_ytm']}"
                    activity_for(ctx).start_thread(playlist_navigation.navigate_when_ready,
                        args=(client, bridge, res["new_ytm"], watch_url))
                # #48: stash the full canvas so this playlist can be reopened in Clusters and regrown a
                # different way. Stored verbatim (validated as JSON); absent for older clients / failures.
                state = form.get("state")
                if state:
                    try:
                        json.loads(state)
                        store.playlist_recipes.set_cluster_canvas(res["new_ytm"], state, now_fn())
                    except (ValueError, TypeError):
                        pass
            except PlaylistAddUncertain as exc:
                result.update(error=str(exc), partial_ytm=exc.playlist_id)
            except Exception:  # noqa: BLE001 - surface a friendly card, log the detail
                ctx.logger.exception("save cluster %r failed", name)
                result["error"] = "YouTube returned an unexpected response."
        resp = templates.TemplateResponse(request, "_partials/generated_result.html", result)
        if not result.get("error"):
            resp.headers["HX-Trigger"] = "cluster-saved"   # tell the canvas to clear (it was created)
        return resp

    return router
