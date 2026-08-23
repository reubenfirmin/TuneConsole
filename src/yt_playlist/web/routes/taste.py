"""Tools > Taste Model: full visibility into the recommendation model + tinkering controls."""
import json
from fastapi import APIRouter, Request
from fastapi.responses import Response

from yt_playlist.rec import autotune_run, eval_recs, rec_params, recommend, rose, taste_viz
from yt_playlist.rec.rec_dao import RecDao
from yt_playlist.web.context import form_float


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates = ctx.store, ctx.templates
    comparison_setting = "taste_comparison_baseline_v1"

    # The three multiplier layers of a facet's chain, in the order the ranker composes them:
    # lasting bias (permanent weight x standing lean), the transient tilt, and their product.
    CHAIN = (("lasting", "perm"), ("transient_mult", "tran"), ("effective", "eff"))
    # A +/-15% multiplier fills a deviation rose. Below that the scale stays fixed, so a nearly
    # neutral model draws small petals instead of being normalized up to full amplitude; above it the
    # scale expands to fit. All three layers of one card share the scale, so their shapes compare.
    CHAIN_SCALE_MIN = 0.15

    def _chain_rows(rows, n=10, roses=True):
        """Augment the top-n axis rows in place with the geometry for the multiplier chain: `frac_*`
        in [-1, 1] for the diverging bars, and (when `roses`) `petal` for the share rose plus
        `petal_perm` / `petal_tran` / `petal_eff` for the three layer roses. Every layer of one card
        is measured against ONE shared scale, so a petal that is twice as long really is twice the
        tilt - and `effective`'s petal visibly composes the other two."""
        rows = rows[:n]
        if not rows:
            return rows
        scale = max(CHAIN_SCALE_MIN,
                    max(abs(r[key] - 1.0) for r in rows for key, _ in CHAIN))
        for r in rows:
            for key, short in CHAIN:
                r[f"frac_{short}"] = max(-1.0, min(1.0, (r[key] - 1.0) / scale))
        if roses:
            geom = {short: rose.rose_geometry_deviation([r[key] - 1.0 for r in rows], scale=scale)
                    for key, short in CHAIN}
            unsigned = rose.rose_geometry([r["share"] for r in rows])
            for i, r in enumerate(rows):
                r["petal"] = unsigned[i]
                for _key, short in CHAIN:
                    r[f"petal_{short}"] = geom[short][i]
        return rows

    def _viz_context():
        viz = taste_viz.model_transparency(store, ctx.now())
        viz["genres"] = _chain_rows(viz["genres"])
        viz["eras"] = _chain_rows(viz["eras"])
        viz["artists"] = _chain_rows(viz["artists"], n=12, roses=False)
        return {"viz": viz}

    MODE_NEW_S = 2 * 86400.0   # a mode first seen within this window renders as "new"

    def _modes_context():
        now = ctx.now()
        rows = store.modes.list_modes(active_only=True)
        rep_keys = [k for m in rows for k in m["rep_keys"]]
        meta = store.modes.meta_for(rep_keys)
        from yt_playlist.rec import mode_eval
        board = {b["mode_id"]: b for b in mode_eval.mode_scoreboard(store)}
        modes = []
        for m in rows:
            b = board.get(m["mode_id"], {})
            tracks = [meta[k] for k in m["rep_keys"] if k in meta]
            modes.append({"label": m["label"], "size": m["size"],
                          "fresh": (now - m["first_seen"]) < MODE_NEW_S, "tracks": tracks,
                          "offered": b.get("offered", 0), "picked": b.get("picked", 0),
                          "plays": b.get("plays", 0)})
        return {"modes": modes}

    def _refresh():
        return Response(status_code=200, headers={"HX-Refresh": "true"})

    def _stale():
        # Save without reloading the page: just flag the live sample as out of date (the user
        # refreshes it manually). Avoids the full-page reload that made every bar re-animate.
        return Response(status_code=200, headers={"HX-Trigger": "taste-stale"})

    def _param_view(spec):
        return {"name": spec.name, "label": spec.label, "help": spec.explanation,
                "min": spec.min, "max": spec.max, "step": spec.step, "default": spec.default,
                "boolean": spec.boolean, "value": rec_params.get_param(store, spec.name)}

    def _group(g):
        return {"main": [_param_view(s) for s in rec_params.PARAMS if s.group == g and not s.advanced],
                "advanced": [_param_view(s) for s in rec_params.PARAMS if s.group == g and s.advanced]}

    def _model_context():
        bd = recommend.taste_breadth(store)
        dao = RecDao(store)
        tracks_total = len(store.get_playlists()) and dao.tracks_total()
        weights = store.get_weights(now=ctx.now(), revert_halflife_d=rec_params.get_param(store, "weight_revert_halflife_d"))
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
            "params": [_param_view(s) for s in rec_params.PARAMS if not s.advanced and s.group == "discovery"],
            "advanced_params": [_param_view(s) for s in rec_params.PARAMS if s.advanced and s.group == "discovery"],
            "groups": {"transient": _group("transient"), "graduation": _group("graduation")},
            "feedback": dao.feedback_summary(),
            "bans": [{"key": r["item_key"], "until": r["until"]} for r in store.list_dislikes()],
            "autotune_result": autotune_run.last_result(store),
        }

    @router.get("/taste")
    def taste_page(request: Request):
        return templates.TemplateResponse(
            request, "taste.html", {**_model_context(), **_viz_context(), **_modes_context()})

    @router.get("/taste/viz/engine")
    def taste_viz_engine(request: Request):
        # The expensive transparency panels (embedding recall + per-playlist taste contexts load
        # vectors; the centroid tilt re-derives the mood direction) stream in lazily, like /taste/recall.
        return templates.TemplateResponse(request, "_partials/taste_viz_engine.html",
                                          {"engine": taste_viz.engine_panel(store),
                                           "tilt": taste_viz.centroid_tilt_panel(store, ctx.now())})

    @router.get("/taste/recall")
    def taste_recall(request: Request):
        # the expensive stats, lazy-loaded so the page paints instantly. This is the §1 model-health
        # panel: warm-path recall@k, leakage-free rolling temporal recall, cold-path projection (with
        # its failure-mode breakdown), and graduation counts by source from the §1c log.
        holdout_days = eval_recs.best_holdout(store)
        return templates.TemplateResponse(request, "_partials/taste_recall.html",
                                          {"recall": eval_recs.recall_at_k(store, k=20),
                                           "proj": eval_recs.projection_recall(store, k=20),
                                           "temporal": eval_recs.rolling_temporal_recall(
                                               store, holdout_days=holdout_days, k=20),
                                           "holdout_days": holdout_days,
                                           "grad_counts": store.graduation_log_counts()})

    @router.get("/taste/preview")
    def taste_preview(request: Request):
        # Manual-refresh live sample of For You under the current knobs (recomputed only on demand).
        # A random slice of the matching pool (not the top-N) so every refresh shows a new set,
        # even when the knobs haven't changed.
        items = recommend.taste_sample(store, ctx.now(), limit=8)
        return templates.TemplateResponse(request, "_partials/taste_preview.html", {"items": items})

    def _comparison_context():
        raw = store.get_setting(comparison_setting)
        if not raw:
            return {"comparison": None}
        try:
            baseline = json.loads(raw)
        except (TypeError, ValueError):
            return {"comparison": None}
        current = recommend.taste_comparison(store, ctx.now(), [row["key"] for row in baseline])
        before = {row["key"]: row for row in baseline}
        rows = []
        for row in current:
            old = before[row["key"]]
            row["old_rank"] = old["rank"]
            row["old_score"] = old["score"]
            row["rank_delta"] = old["rank"] - row["rank"]
            row["score_delta"] = row["score"] - old["score"]
            rows.append(row)
        return {"comparison": rows}

    @router.post("/taste/comparison/baseline")
    def taste_comparison_baseline(request: Request):
        baseline = recommend.taste_comparison(store, ctx.now())
        compact = [{"key": row["key"], "rank": row["rank"], "score": row["score"]}
                   for row in baseline]
        store.set_setting(comparison_setting, json.dumps(compact))
        return templates.TemplateResponse(request, "_partials/taste_comparison.html",
                                          _comparison_context())

    @router.get("/taste/comparison")
    def taste_comparison(request: Request):
        return templates.TemplateResponse(request, "_partials/taste_comparison.html",
                                          _comparison_context())

    @router.post("/taste/weight")
    async def taste_weight(request: Request):
        form = await request.form()
        axis, weight = form.get("axis"), form_float(form.get("weight"))
        if axis and weight is not None:
            if axis.startswith("genre:"):     # genre weights use the [0,2] band so a family can be muted
                store.set_weight(axis, weight, lo=rec_params.GENRE_MIN, hi=rec_params.GENRE_MAX, now=ctx.now())
            else:
                store.set_weight(axis, weight, now=ctx.now())
        return _stale()

    @router.post("/taste/param")
    async def taste_param(request: Request):
        form = await request.form()
        name, val = form.get("name"), form.get("value")
        if name in rec_params.PARAMS_BY_NAME:
            spec = rec_params.PARAMS_BY_NAME[name]
            if spec.boolean:
                rec_params.set_param(store, name, val is not None)   # unchecked box sends no value
            elif val not in (None, ""):
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

    import threading
    _autotune_lock = threading.Lock()
    _autotune_state = {"running": False}

    def _autotune_status(request):
        running = _autotune_state["running"]
        resp = templates.TemplateResponse(request, "_partials/autotune_status.html",
                                          {"running": running,
                                           "autotune_result": autotune_run.last_result(store)})
        if not running:
            # The poll's final (done) response tells the Model status card to refresh its vector
            # count / dim and recompute recall@20 against the freshly tuned model.
            resp.headers["HX-Trigger"] = "autotune-done"
        return resp

    @router.post("/taste/autotune")
    def taste_autotune(request: Request):
        """Grid-search the embedding dim by recall@k and rebuild on the winner (background)."""
        with _autotune_lock:
            if not _autotune_state["running"]:
                _autotune_state["running"] = True

                def run():
                    try:
                        autotune_run.run_and_record(store, ctx.now())
                    except Exception:  # noqa: BLE001 - never crash the app on a tune failure
                        ctx.logger.warning("autotune run failed", exc_info=True)
                    finally:
                        _autotune_state["running"] = False

                threading.Thread(target=run, daemon=True).start()
        return _autotune_status(request)

    @router.get("/taste/autotune-status")
    def taste_autotune_status(request: Request):
        return _autotune_status(request)

    @router.get("/taste/model-status")
    def taste_model_status(request: Request):
        # Re-rendered when Auto-tune finishes (autotune-done) so the stat-grid reflects the new model.
        return templates.TemplateResponse(request, "_partials/model_status.html", _model_context())

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

    return router
