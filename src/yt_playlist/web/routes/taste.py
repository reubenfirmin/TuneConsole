"""Tools > Taste Model: full visibility into the recommendation model + tinkering controls."""
from fastapi import APIRouter, Request
from fastapi.responses import Response

from yt_playlist.rec import embed, eval_recs, rec_params, recommend, rose, taste_viz
from yt_playlist.rec.rec_dao import RecDao


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates = ctx.store, ctx.templates

    def _rose_rows(rows, n=10):
        """Augment the top-n axis rows in place with `petal` (share, unsigned) and `petal_t`
        (transient lean, signed-diverging) SVG geometry, so the template stays logic-light."""
        rows = rows[:n]
        unsigned = rose.rose_geometry([r["share"] for r in rows])
        signed = rose.rose_geometry_signed([r["transient_lean"] for r in rows])
        for r, p, pt in zip(rows, unsigned, signed):
            r["petal"], r["petal_t"] = p, pt
        return rows

    def _viz_context():
        viz = taste_viz.model_transparency(store, ctx.now())
        viz["genres"] = _rose_rows(viz["genres"])
        viz["eras"] = _rose_rows(viz["eras"])
        return {"viz": viz}

    def _refresh():
        return Response(status_code=200, headers={"HX-Refresh": "true"})

    def _stale():
        # Save without reloading the page: just flag the live sample as out of date (the user
        # refreshes it manually). Avoids the full-page reload that made every bar re-animate.
        return Response(status_code=200, headers={"HX-Trigger": "taste-stale"})

    def _param_view(spec):
        return {"name": spec.name, "label": spec.label, "help": spec.explanation,
                "min": spec.min, "max": spec.max, "step": spec.step, "default": spec.default,
                "value": rec_params.get_param(store, spec.name)}

    def _model_context():
        bd = recommend.taste_breadth(store)
        dao = RecDao(store)
        tracks_total = len(store.get_playlists()) and dao.tracks_total()
        weights = store.get_weights()
        families = sorted(bd["families"].items(), key=lambda x: -x[1])
        return {
            "vectors": store.rec_vectors_count(),
            "tracks_total": tracks_total,
            "tagged": bd["n_tagged"],
            "coverage": (bd["n_tagged"] / tracks_total) if tracks_total else 0.0,
            "breadth": bd["breadth"],
            "baskets": len(store.rec_baskets()),
            "lanes": [{"name": n, "label": lbl, "help": h, "value": weights.get(f"lane:{n}", 1.0)}
                      for n, lbl, h in rec_params.LANES],
            "lane_min": rec_params.LANE_MIN, "lane_max": rec_params.LANE_MAX,
            "lane_default": rec_params.LANE_DEFAULT, "lane_step": 0.05,
            "genres": [{"family": f, "share": share, "weight": weights.get(f"genre:{f}", 1.0)}
                       for f, share in families],
            "genre_min": rec_params.GENRE_MIN, "genre_max": rec_params.GENRE_MAX,
            "genre_default": rec_params.GENRE_DEFAULT, "genre_step": rec_params.GENRE_STEP,
            "params": [_param_view(s) for s in rec_params.PARAMS if not s.advanced],
            "advanced_params": [_param_view(s) for s in rec_params.PARAMS if s.advanced],
            "feedback": dao.feedback_summary(),
            "bans": [{"key": r["item_key"], "until": r["until"]} for r in store.list_dislikes()],
        }

    @router.get("/taste")
    def taste_page(request: Request):
        return templates.TemplateResponse(request, "taste.html", {**_model_context(), **_viz_context()})

    @router.get("/taste/viz/engine")
    def taste_viz_engine(request: Request):
        # The expensive transparency panels (embedding recall + per-playlist taste contexts load
        # vectors; the centroid tilt re-derives the mood direction) stream in lazily, like /taste/recall.
        return templates.TemplateResponse(request, "_partials/taste_viz_engine.html",
                                          {"engine": taste_viz.engine_panel(store),
                                           "tilt": taste_viz.centroid_tilt_panel(store, ctx.now())})

    @router.get("/taste/recall")
    def taste_recall(request: Request):
        # the expensive stats — lazy-loaded so the page paints instantly
        return templates.TemplateResponse(request, "_partials/taste_recall.html",
                                          {"recall": eval_recs.recall_at_k(store, k=20),
                                           "proj": eval_recs.projection_recall(store, k=20)})

    @router.get("/taste/preview")
    def taste_preview(request: Request):
        # Manual-refresh live sample of For You under the current knobs (recomputed only on demand).
        # A random slice of the matching pool — not the top-N — so every refresh shows a new set,
        # even when the knobs haven't changed.
        items = recommend.taste_sample(store, ctx.now(), limit=8)
        return templates.TemplateResponse(request, "_partials/taste_preview.html", {"items": items})

    @router.post("/taste/weight")
    async def taste_weight(request: Request):
        form = await request.form()
        axis, val = form.get("axis"), form.get("weight")
        if axis and val:
            if axis.startswith("genre:"):     # genre weights use the [0,2] band so a family can be muted
                store.set_weight(axis, float(val), lo=rec_params.GENRE_MIN, hi=rec_params.GENRE_MAX)
            else:
                store.set_weight(axis, float(val))
        return _stale()

    @router.post("/taste/param")
    async def taste_param(request: Request):
        form = await request.form()
        name, val = form.get("name"), form.get("value")
        if name in rec_params.PARAMS_BY_NAME and val not in (None, ""):
            rec_params.set_param(store, name, val)
        return _stale()

    @router.post("/taste/reset-param")
    async def taste_reset_param(request: Request):
        name = (await request.form()).get("name")
        if name in rec_params.PARAMS_BY_NAME:
            rec_params.reset_param(store, name)
        return _stale()

    @router.post("/taste/reset-all")
    def taste_reset_all():
        # Everything back to defaults: lane + genre weights and every scalar knob. Full reload so the
        # sliders visibly snap back. Leaves vectors / feedback alone (they have their own controls).
        store.reset_weights()
        rec_params.reset_all_params(store)
        return _refresh()

    @router.post("/taste/autotune")
    def taste_autotune():
        """Grid-search the embedding dim by recall@k and rebuild on the winner."""
        eval_recs.autotune(store)
        return _refresh()

    @router.post("/taste/reset-weights")
    def taste_reset_weights():
        store.reset_weights()
        return _refresh()

    @router.post("/taste/clear-feedback")
    def taste_clear_feedback():
        RecDao(store).clear_feedback()
        return _refresh()

    @router.post("/taste/unban")
    async def taste_unban(request: Request):
        key = (await request.form()).get("key")
        if key:
            store.clear_dislike(key)
        return _refresh()

    def _busy():
        return bool(ctx.rec_worker and ctx.rec_worker.busy)

    def _rebuild_status(request):
        return templates.TemplateResponse(request, "_partials/rebuild_status.html", {"busy": _busy()})

    @router.post("/taste/rebuild")
    def taste_rebuild(request: Request):
        # Background-dispatch (coalesced) so a hung YTM/Last.fm call during materialization can't tie
        # up a request worker. Returns a live status fragment that polls until the worker is idle.
        if ctx.rec_worker:
            ctx.rec_worker.trigger()
        else:
            embed.build_and_store(store)
        return _rebuild_status(request)

    @router.get("/taste/rebuild-status")
    def taste_rebuild_status(request: Request):
        return _rebuild_status(request)

    @router.post("/taste/purge-vectors")
    def taste_purge():
        store.replace_rec_vectors([])
        return _refresh()

    return router
