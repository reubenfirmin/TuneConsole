"""Tools > Taste Model: full visibility into the recommendation model + tinkering controls."""
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from yt_playlist.rec import experiments, rec_params, rose, scorecard
from yt_playlist.web.context import form_float, request_form
from yt_playlist.library.taste_composition import taste as compose_taste


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates = ctx.store, ctx.templates
    taste, queries = compose_taste(store)

    def _checkpoint_context():
        return {"checkpoint": taste.checkpoint()}

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

    def _viz_context(viz):
        viz["genres"] = _chain_rows(viz["genres"])
        viz["eras"] = _chain_rows(viz["eras"])
        viz["artists"] = _chain_rows(viz["artists"], n=12, roses=False)
        return {"viz": viz}

    def _refresh_rankings():
        from yt_playlist.rec import candidate_refresh
        if candidate_refresh.due(store, ctx.now()):
            ctx.rec_worker.trigger_candidate_refresh()

    def _refresh():
        _refresh_rankings()
        return Response(status_code=200, headers={"HX-Refresh": "true"})

    def _stale():
        # Save without reloading the page: just flag the live sample as out of date (the user
        # refreshes it manually). Avoids the full-page reload that made every bar re-animate.
        _refresh_rankings()
        return Response(status_code=200, headers={"HX-Trigger": "taste-stale"})

    def _param_view(spec, controls):
        value = controls['values'][spec.name]
        def display(number):
            return f"{100 * (number - spec.min) / (spec.max - spec.min):g}%" if spec.percentage else number
        param = {"name": spec.name, "label": spec.label, "help": spec.explanation,
                "min": spec.min, "max": spec.max, "step": spec.step, "default": spec.default,
                "value": value, "value_label": display(value),
                "default_label": display(spec.default), "percentage": spec.percentage,
                "low": spec.low, "high": spec.high}
        if spec.name.startswith('album_menu_'):
            tuning = controls['tuning']
            effective = display(tuning['values'][spec.name.removeprefix('album_menu_')])
            duration = f"temporary · {max(1, round(tuning['remaining'] / 60))} min left" if tuning['remaining'] else 'standing setting'
            param['effective_note'] = f'Effective in Albums: {effective} · {duration}'
            param['effective_reset'] = bool(tuning['remaining']) and spec.name == 'album_menu_novelty'
        return param

    @router.post('/taste/reset-album-tuning')
    def reset_album_tuning():
        taste.reset_album_tuning()
        return _refresh()

    def _settings_sections(definitions, param_groups, controls):
        sections = []
        adaptive = controls['adaptive']
        for key, title, tone, note, groups in definitions:
            rendered_groups = []
            for group_title, prefixes in groups:
                params = []
                for spec in rec_params.PARAMS:
                    if spec.name in rec_params.LEGACY_MODE_PARAMS or adaptive and spec.name.startswith('modes_menu_'):
                        continue
                    if (spec.group in param_groups and spec.name.startswith(prefixes)
                            and spec.name not in rec_params.SEMANTIC_PARAMS):
                        param = _param_view(spec, controls)
                        # The feature and subgroup already provide this context.
                        label = spec.label.split(": ", 1)[-1] if param_groups == ("discovery",) else spec.label
                        param["label"] = label[0].upper() + label[1:]
                        params.append(param)
                rendered_groups.append({"title": group_title, "params": params})
            if not any(group['params'] for group in rendered_groups):
                continue
            sections.append({"key": key, "title": title, "tone": tone, "note": note,
                             "groups": rendered_groups,
                             "count": sum(len(g["params"]) for g in rendered_groups)})
        return sections

    def _feature_sections(controls):
        tuning = controls['tuning']
        genres = ', '.join(genre.removeprefix('other:').title() for genre in tuning['genres']) or 'All genres'
        return _settings_sections([
            ("album-menus", "Album menus", "teal",
             "Standing defaults for Home Albums. The Albums tuner overrides them for four hours and does not change lasting taste. "
             f"Current genre filter: {genres}.", [
                 ("Menu choices", ("album_menu_",)),
                 ("Album fit", ("album_collab_",)),
             ]),
            ("home", "Home cards", "teal",
             "Choose how often Home changes and how much variety fits on each card. Limits that are too strict can leave fewer suggestions available.", [
                 ("Refresh & preparation", ("erosion_", "candidate_", "modes_pool_", "modes_cand_")),
                 ("Cards & variety", ("modes_menu_", "modes_artist_", "modes_album_", "fresh_")),
                 ("Track length", ("max_track_",)),
                 ("Comfort listening", ("comfort_",)),
             ]),
            ("discovery", "Finding new music", "blue",
             "Choose how widely Discover searches and how long unadded suggestions stay available. Your library is kept.", [
                 ("", ("discovery_",)),
             ]),
            ("preferences", "Ranking & feedback", "amber",
             "Set how strongly stated preferences affect rankings, how long dislikes exclude tracks, and what counts as mainstream for feedback.", [
                 ("", ("breadth_gain", "axis_weight_cap", "dislike_suppress_", "pop_")),
                 ("Album interests · shared, standing support", ("album_saved_", "album_feedback_", "album_return_")),
                 ("Listening choice measurement", ("choice_",)),
                 ("Mix menu allocation", ("bandit_",)),
             ]),
            ("artists", "Related artists", "blue",
             "Choose how the app finds connections between artists. These settings affect related-artist suggestions.", [
                 ("", ("artist_",)),
             ]),
            ("clusters", "Clusters", "violet",
             "Choose how Clusters finds its next suggestions. Changes apply when you grow a branch.", [
                 ("", ("cluster_",)),
             ]),
            ("ranking", "Experimental ranking", "violet",
             "Try a different way of ordering listening-style cards. It can produce worse picks; use a share of 0 to use standard ranking everywhere.", [
                 ("", ("ppr_",)),
             ]),
            ("cleanup", "Generated playlist cleanup", "danger",
             "Choose how long to keep generated playlists you haven’t listened to much.", [
                 ("", ("generated_",)),
             ]),
        ], ("discovery",), controls)

    def _learning_sections(controls):
        return _settings_sections([
            ("influence", "Recent influence", "blue",
             "Set how skips, mood choices, and recent genre, era, and artist preferences shift current picks. Overall influence from plays, likes, and sound is in Recommendations, under Main controls.", [
                 ("", ("skip_transient_w", "facet_gain", "mood_alpha", "facet_mult_")),
             ]),
            ("memory", "Listening memory", "teal",
             "Choose how long recent activity matters and how much history to consider. Each half-life is the time until a signal has half its original influence.", [
                 ("", ("play_halflife_d", "mood_halflife_d", "like_halflife_d", "skip_halflife_d",
                       "recent_play_limit", "recent_horizon_days")),
             ]),
            ("session", "Current session", "violet",
             "Set how much the current session and earlier listening today influence picks.", [
                 ("", ("now_min_events", "now_gain", "session_")),
             ]),
        ], ("transient",), controls)

    def _model_context():
        return queries.model_status()

    def _autotune_context(data=None, *, limit=6):
        data = queries.selection(ctx.now(), limit=limit) if data is None else data
        result = data['autotune_result']
        if result and result.get("ran_at") is not None:
            ran_at = datetime.fromtimestamp(result["ran_at"], timezone.utc)
            result = {**result, "ran_at_iso": ran_at.isoformat(),
                      "ran_at_label": ran_at.strftime("%b %d, %Y at %H:%M UTC")}
        return {**data, 'autotune_result': result}

    def _feedback_context(data):
        now = ctx.now()
        tracks = []
        for track in data['bans']:
            until = track['until']
            expiry = datetime.fromtimestamp(until, timezone.utc) if until is not None else None
            tracks.append({**track,
                           "until_iso": expiry.isoformat() if expiry else None,
                           "until_label": ("Expired" if until <= now else expiry.strftime("%b %d, %Y"))
                                          if expiry else "No expiry"})
        return {**data, 'bans': tracks}

    def _recommendation_context(data):
        controls = data.pop('controls')
        return {**data,
            "semantic_params": [_param_view(rec_params.PARAMS_BY_NAME[name], controls)
                                for name in rec_params.SEMANTIC_PARAMS],
            "discovery_sections": _feature_sections(controls),
        }

    @router.get("/taste")
    def taste_page(request: Request):
        # The shell does no model work. Only the visible tab requests its data, including on a
        # direct hash link; switching back keeps the loaded DOM and unsaved input positions.
        return templates.TemplateResponse(request, "taste.html")

    @router.get("/taste/panels/{panel}")
    def taste_panel(request: Request, panel: str):
        if panel == "experiment":
            data = _checkpoint_context()
        else:
            if panel not in queries.PANELS:
                raise HTTPException(status_code=404)
            data = queries.panel(panel, ctx.now())
        if panel == 'overview':
            data['mode_refreshing'] = bool(ctx.rec_worker and ctx.rec_worker.busy)
        elif panel == 'profile':
            data.update(_viz_context(data['viz']))
        elif panel == 'recommendations':
            data = _recommendation_context(data)
        elif panel == 'learning':
            data['learning_sections'] = _learning_sections(data.pop('controls'))
        elif panel == 'health':
            data = _autotune_context(data)
        elif panel == 'feedback':
            data = _feedback_context(data)
        return templates.TemplateResponse(request, "_partials/taste_panel.html", {"panel": panel, **data})

    @router.get("/taste/checkpoint")
    def taste_checkpoint(request: Request):
        return templates.TemplateResponse(request, "_partials/taste_checkpoint.html", _checkpoint_context())

    @router.get('/taste/grouping-preview')
    def grouping_preview(request: Request):
        return templates.TemplateResponse(request, '_partials/taste_grouping_preview.html',
                                          queries.grouping())

    @router.post("/taste/checkpoint")
    def taste_save_checkpoint(request: Request):
        taste.save_checkpoint(ctx.now(), replace=True)
        return templates.TemplateResponse(request, "_partials/taste_checkpoint.html", _checkpoint_context())

    @router.post("/taste/checkpoint/restore")
    def taste_restore_checkpoint():
        taste.restore_checkpoint()
        return _refresh()

    @router.get("/taste/viz/recent-sound")
    def taste_recent_sound(request: Request):
        return templates.TemplateResponse(request, "_partials/taste_recent_sound.html",
                                          queries.recent_sound(ctx.now()))

    @router.get("/taste/viz/playlist-influence")
    def taste_playlist_influence(request: Request):
        return templates.TemplateResponse(request, "_partials/taste_playlist_influence.html",
                                          queries.playlist_influence())

    @router.get("/taste/recall")
    def taste_recall(request: Request):
        now = ctx.now()
        if ctx.rec_worker:
            ctx.rec_worker.trigger_health_refresh()
        return templates.TemplateResponse(request, "_partials/taste_recall.html",
                                          queries.recall(now, failed=bool(
                                              ctx.rec_worker and ctx.rec_worker.health_refresh_failed)))

    @router.get("/taste/preview")
    def taste_preview(request: Request):
        # Manual-refresh live sample of For You under the current knobs (recomputed only on demand).
        # A random slice of the matching pool (not the top-N) so every refresh shows a new set,
        # even when the knobs haven't changed.
        return templates.TemplateResponse(request, "_partials/taste_preview.html", queries.preview(ctx.now()))

    @router.get("/taste/scorecard")
    def served_scorecard(start: float | None = None, end: float | None = None):
        end = ctx.now() if end is None else end
        start = end - 30 * 86400 if start is None else start
        if not (0 <= start < end and end - start <= 90 * 86400):
            raise HTTPException(422, "Choose an ordered date range of at most 90 days.")
        return scorecard.recommendation_scorecard(store, start, end)

    @router.get("/taste/served/{impression_id}")
    def served_trace(impression_id: int):
        trace = scorecard.served_recommendation(store, impression_id)
        if trace is None:
            raise HTTPException(404, "Recommendation impression not found.")
        return trace

    @router.get("/taste/experiments")
    def experiment_report(start: float | None = None, end: float | None = None):
        now = ctx.now()
        end = now if end is None else end
        start = max(0, end - 30 * 86400) if start is None else start
        if not (0 <= start < end <= now and end - start <= 90 * 86400):
            raise HTTPException(422, "Choose a past date range of at most 90 days.")
        return {**scorecard.experiment_report(store, start, end, now), **experiments.lifecycle(store),
                "decisions": experiments.decisions(store)}

    @router.post("/taste/experiments/decision")
    def experiment_decision(request: Request, form=Depends(request_form)):
        try:
            return experiments.decide(store, form.get("decision"), form.get("cohort", ""),
                                      form.get("reason", ""), ctx.now())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    def _comparison_context():
        return queries.comparison(ctx.now())

    @router.post("/taste/comparison/baseline")
    def taste_comparison_baseline(request: Request):
        taste.capture_baseline(ctx.now())
        return templates.TemplateResponse(request, "_partials/taste_comparison.html",
                                          {**_comparison_context(), "baseline_captured": True})

    @router.get("/taste/comparison")
    def taste_comparison(request: Request):
        return templates.TemplateResponse(request, "_partials/taste_comparison.html",
                                          _comparison_context())

    @router.post("/taste/weight")
    def taste_weight(request: Request, form=Depends(request_form)):
        axis, weight = form.get("axis"), form_float(form.get("weight"))
        if axis and weight is not None:
            if axis.startswith("lane:") and axis.removeprefix("lane:") not in rec_params.LANE_NAMES:
                raise HTTPException(status_code=400, detail="Unknown recommendation source")
            taste.set_weight(axis, weight, ctx.now())
        return _stale()

    @router.post("/taste/param")
    def taste_param(request: Request, form=Depends(request_form)):
        name, val = form.get("name"), form.get("value")
        if name in rec_params.PARAMS_BY_NAME:
            taste.set_param(name, val, ctx.now())
        return _stale()

    @router.post("/taste/reset-param")
    def taste_reset_param(request: Request, form=Depends(request_form)):
        name = form.get("name")
        if name in rec_params.PARAMS_BY_NAME:
            taste.reset_param(name, ctx.now())
        return _stale()

    @router.post("/taste/reset-all")
    def taste_reset_all():
        # Everything back to defaults: lane + genre weights and every scalar knob. Full reload so the
        # sliders visibly snap back. Leaves vectors / feedback alone (they have their own controls).
        taste.reset_all(ctx.now())
        return _refresh()

    def _autotune_status(request):
        data = _autotune_context()
        resp = templates.TemplateResponse(request, "_partials/autotune_status.html",
                                          data)
        if (data["selection"]["state"] not in ("running", "restoring")
                and request.query_params.get("version") != data["selection"]["version"]):
            # The poll's final (done) response tells the Model status card to refresh its vector
            # count / dim and recompute recall@20 against the freshly tuned model.
            resp.headers["HX-Trigger"] = "autotune-done"
        return resp

    @router.post("/taste/autotune")
    def taste_autotune(request: Request):
        raise HTTPException(status_code=409, detail="Model selection now runs automatically when there is enough listening evidence.")

    @router.post("/taste/model-selection/restore")
    def restore_model_selection(request: Request):
        try:
            taste.restore_selection()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        ctx.rec_worker.trigger()
        return _autotune_status(request)

    @router.post("/taste/model-selection/pause")
    def pause_model_selection(request: Request):
        taste.pause_selection()
        ctx.rec_worker.trigger()
        return _autotune_status(request)

    @router.post("/taste/model-selection/resume")
    def resume_model_selection(request: Request):
        try:
            taste.resume_selection()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        ctx.rec_worker.trigger()
        return _autotune_status(request)

    @router.get("/taste/autotune-status")
    def taste_autotune_status(request: Request):
        return _autotune_status(request)

    @router.get("/taste/model-selection/history")
    def model_selection_history(request: Request):
        return templates.TemplateResponse(request, "model_selection_history.html", _autotune_context(limit=100))

    @router.get("/taste/model-status")
    def taste_model_status(request: Request):
        # Re-rendered when Auto-tune finishes (autotune-done) so the stat-grid reflects the new model.
        return templates.TemplateResponse(request, "_partials/model_status.html", _model_context())

    @router.get('/taste/content-maintenance')
    def taste_content_maintenance(request: Request):
        return templates.TemplateResponse(request, '_partials/content_maintenance.html',
                                          queries.maintenance())

    @router.post("/taste/reset-weights")
    def taste_reset_weights():
        taste.reset_weights(ctx.now())
        return _refresh()

    @router.post("/taste/clear-feedback")
    def taste_clear_feedback():
        taste.clear_feedback()
        return _refresh()

    @router.post("/taste/unban")
    def taste_unban(request: Request, form=Depends(request_form)):
        key = form.get("key")
        if key:
            taste.unban(key, ctx.now())
        return _refresh()

    return router
