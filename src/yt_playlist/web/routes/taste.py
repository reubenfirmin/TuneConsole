"""Tools > Taste Model: full visibility into the recommendation model + tinkering controls."""
import json
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from yt_playlist.rec import autotune_run, eval_recs, model_selection, rec_params, recommend, rose, taste_viz
from yt_playlist.rec.rec_dao import RecDao
from yt_playlist.web.context import form_float


def build(ctx) -> APIRouter:
    router = APIRouter()
    store, templates = ctx.store, ctx.templates
    comparison_setting = "taste_comparison_baseline_v2"
    checkpoint_setting = "taste_settings_checkpoint_v1"
    param_keys = tuple(rec_params.SETTING_PREFIX + p.name for p in rec_params.PARAMS)

    def _checkpoint_context():
        raw = store.get_setting(checkpoint_setting)
        checkpoint = json.loads(raw) if raw else None
        if checkpoint:
            checkpoint["saved_at_iso"] = datetime.fromtimestamp(checkpoint["saved_at"], timezone.utc).isoformat()
        return {"checkpoint": checkpoint}

    def _save_starting_point(replace=False):
        store.rec.model.save_taste_settings(checkpoint_setting, param_keys, ctx.now(), replace=replace)

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
        meta = store.tracks_by_keys(rep_keys)
        modes = []
        for m in rows:
            tracks = [meta[k] for k in m["rep_keys"] if k in meta]
            modes.append({"label": m["label"], "size": m["size"],
                          "fresh": (now - m["first_seen"]) < MODE_NEW_S, "tracks": tracks})
        return {"modes": modes}

    def _refresh():
        return Response(status_code=200, headers={"HX-Refresh": "true"})

    def _stale():
        # Save without reloading the page: just flag the live sample as out of date (the user
        # refreshes it manually). Avoids the full-page reload that made every bar re-animate.
        return Response(status_code=200, headers={"HX-Trigger": "taste-stale"})

    def _param_view(spec):
        value = rec_params.get_param(store, spec.name)
        def display(number):
            return f"{100 * (number - spec.min) / (spec.max - spec.min):g}%" if spec.percentage else number
        return {"name": spec.name, "label": spec.label, "help": spec.explanation,
                "min": spec.min, "max": spec.max, "step": spec.step, "default": spec.default,
                "boolean": spec.boolean, "value": value, "value_label": display(value),
                "default_label": display(spec.default), "percentage": spec.percentage,
                "low": spec.low, "high": spec.high}

    def _settings_sections(definitions, param_groups):
        sections = []
        for key, title, tone, note, groups in definitions:
            rendered_groups = []
            for group_title, prefixes in groups:
                params = []
                for spec in rec_params.PARAMS:
                    if (spec.group in param_groups and spec.name.startswith(prefixes)
                            and spec.name not in rec_params.SEMANTIC_PARAMS):
                        param = _param_view(spec)
                        # The feature and subgroup already provide this context.
                        label = spec.label.split(": ", 1)[-1] if param_groups == ("discovery",) else spec.label
                        param["label"] = label[0].upper() + label[1:]
                        params.append(param)
                rendered_groups.append({"title": group_title, "params": params})
            sections.append({"key": key, "title": title, "tone": tone, "note": note,
                             "groups": rendered_groups,
                             "count": sum(len(g["params"]) for g in rendered_groups)})
        return sections

    def _feature_sections():
        return _settings_sections([
            ("home", "Home cards", "teal",
             "Choose how often Home changes and how much variety fits on each card. Limits that are too strict can leave fewer suggestions available.", [
                 ("Refresh & preparation", ("erosion_", "candidate_", "modes_pool_", "modes_cand_")),
                 ("Cards & variety", ("modes_menu_", "modes_artist_", "modes_album_", "fresh_")),
                 ("Track length", ("max_track_",)),
                 ("Comfort listening", ("comfort_",)),
                 ("Getting started", ("onboard_",)),
             ]),
            ("styles", "Listening-style grouping", "violet",
             "Choose how tracks are grouped into listening styles and how those groups carry over when the model rebuilds.", [
                 ("", ("modes_k", "modes_min_", "modes_match_")),
             ]),
            ("discovery", "Finding new music", "blue",
             "Choose how widely Discover searches and how long unadded suggestions stay available. Your library is kept.", [
                 ("", ("discovery_", "palette_")),
             ]),
            ("preferences", "Ranking & feedback", "amber",
             "Set how strongly stated preferences affect rankings, how long dislikes exclude tracks, and what counts as mainstream for feedback.", [
                 ("", ("breadth_gain", "axis_weight_cap", "dislike_suppress_", "pop_")),
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
        ], ("discovery",))

    def _learning_sections():
        return _settings_sections([
            ("influence", "Recent influence", "blue",
             "Set how dislikes, skips, mood choices, and recent genre, era, and artist preferences shift current picks. Overall influence from plays, likes, and sound is in Recommendations, under Main controls.", [
                 ("", ("dislike_transient_w", "skip_transient_w", "facet_gain", "mood_alpha", "facet_mult_")),
             ]),
            ("memory", "Listening memory", "teal",
             "Choose how long recent activity matters and how much history to consider. Each half-life is the time until a signal has half its original influence.", [
                 ("", ("play_halflife_d", "mood_halflife_d", "like_halflife_d", "dislike_halflife_d", "skip_halflife_d", "recent_play_limit")),
             ]),
            ("session", "Current session", "violet",
             "Control how Home recognizes your current listening style and how strongly the session carries over between plays.", [
                 ("", ("now_", "session_")),
             ]),
            ("saved", "Saved preferences", "amber",
             "Choose when repeated activity changes saved taste and how large each change is. Saved genre and mix adjustments also fade toward neutral over time, including manual changes.", [
                 ("Learning behavior", ("theme_threshold", "graduate_")),
                 ("Adjustment memory", ("weight_revert_halflife_d",)),
             ]),
            ("rates", "Learning rates", "blue",
             "Choose how much each source contributes to learning saved preferences. Higher values accumulate evidence faster; 0 stops that source from contributing to learning.", [
                 ("", ("source_w_",)),
             ]),
        ], ("transient", "graduation"))

    def _model_context():
        bd = recommend.taste_breadth(store)
        dao = RecDao(store)
        tracks_total = dao.tracks_total()
        tagged = sum(dao.genre_distribution().values())
        return {
            **taste_viz.engine_panel(store),
            "tracks_total": tracks_total,
            "tagged": tagged,
            "coverage": (tagged / tracks_total) if tracks_total else 0.0,
            "breadth": bd["breadth"],
            "breadth_tagged": bd["n_tagged"],
            "breadth_families": bd["n_families"],
            "baskets": len(store.rec_baskets()),
        }

    def _autotune_context(*, limit=6):
        result = autotune_run.last_result(store)
        if result and result.get("ran_at") is not None:
            ran_at = datetime.fromtimestamp(result["ran_at"], timezone.utc)
            result = {**result, "ran_at_iso": ran_at.isoformat(),
                      "ran_at_label": ran_at.strftime("%b %d, %Y at %H:%M UTC")}
        return {"selection": model_selection.view(store, ctx.now(), limit=limit), "autotune_result": result}

    def _feedback_context():
        now = ctx.now()
        bans = store.list_dislikes()
        keys = [row["item_key"] for row in bans]
        metadata = store.tracks_by_keys(keys)
        metadata.update(store.discovered_tracks_by_keys([key for key in keys if key not in metadata]))
        tracks = []
        for row in bans:
            key = row["item_key"]
            title, _, artist = key.partition("|")
            track = {"title": title or "Unknown track", "artist": artist or "Unknown artist",
                     "album": "", "thumbnail": None, "video_id": None, **metadata.get(key, {})}
            until = row["until"]
            expiry = datetime.fromtimestamp(until, timezone.utc) if until is not None else None
            tracks.append({**track, "key": key, "until": until,
                           "until_iso": expiry.isoformat() if expiry else None,
                           "until_label": ("Expired" if until <= now else expiry.strftime("%b %d, %Y"))
                                          if expiry else "No expiry"})
        return {"feedback": {kind: count for kind, count in store.rec.model.feedback_summary().items()
                             if not kind.endswith("_grad")}, "bans": tracks}

    def _recommendation_context():
        bd = recommend.taste_breadth(store)
        now = ctx.now()
        weights = store.get_weights(now=now, revert_halflife_d=rec_params.get_param(store, "weight_revert_halflife_d"))
        records = store.weight_records()
        families = sorted(bd["families"].items(), key=lambda x: -x[1])
        genres = []
        for family, share in families:
            record = records.get(f"genre:{family}")
            updated = record["updated_at"] if record else None
            genres.append({"family": family, "share": share,
                           "weight": weights.get(f"genre:{family}", 1.0),
                           "has_record": record is not None,
                           "saved_weight": record["weight"] if record else 1.0,
                           "age_days": max(0.0, (now - updated) / 86400) if updated is not None else None})
        return {
            "genres": genres,
            "genre_min": rec_params.GENRE_MIN, "genre_max": rec_params.GENRE_MAX,
            "genre_default": rec_params.GENRE_DEFAULT, "genre_step": rec_params.GENRE_STEP,
            "semantic_params": [_param_view(rec_params.PARAMS_BY_NAME[name])
                                for name in rec_params.SEMANTIC_PARAMS],
            "discovery_sections": _feature_sections(),
        }

    @router.get("/taste")
    def taste_page(request: Request):
        # The shell does no model work. Only the visible tab requests its data, including on a
        # direct hash link; switching back keeps the loaded DOM and unsaved input positions.
        return templates.TemplateResponse(request, "taste.html")

    @router.get("/taste/panels/{panel}")
    def taste_panel(request: Request, panel: str):
        if panel == "overview":
            dao = RecDao(store)
            total = dao.tracks_total()
            choices = taste_viz.mix_choices(store)
            profiles = {r["mode_id"]: r["profile"] for r in choices["rows"]}
            data = {"viz": taste_viz.overview(store, ctx.now(), profiles=profiles),
                    "mode_refreshing": bool(ctx.rec_worker and ctx.rec_worker.busy),
                    "generation": {"choices": choices},
                    "vectors": store.rec_vectors_count(),
                    "coverage": sum(dao.genre_distribution().values()) / total if total else 0.0}
        elif panel == "experiment":
            data = _checkpoint_context()
        elif panel == "profile":
            data = {**_viz_context(), **_modes_context()}
        elif panel == "generated":
            data = {"viz": taste_viz.source_weights(store, ctx.now()),
                    "generation": taste_viz.generated_playlists(store, ctx.now()),
                    "lane_min": rec_params.LANE_MIN, "lane_max": rec_params.LANE_MAX,
                    "lane_default": rec_params.LANE_DEFAULT, "lane_step": 0.05}
        elif panel == "recommendations":
            data = _recommendation_context()
        elif panel == "learning":
            data = {"learning": taste_viz.learning_progress(store, ctx.now()),
                    "learning_sections": _learning_sections()}
        elif panel == "health":
            data = {**_model_context(), **_autotune_context()}
        elif panel == "feedback":
            data = _feedback_context()
        else:
            raise HTTPException(status_code=404)
        return templates.TemplateResponse(request, "_partials/taste_panel.html", {"panel": panel, **data})

    @router.get("/taste/checkpoint")
    def taste_checkpoint(request: Request):
        return templates.TemplateResponse(request, "_partials/taste_checkpoint.html", _checkpoint_context())

    @router.post("/taste/checkpoint")
    def taste_save_checkpoint(request: Request):
        _save_starting_point(replace=True)
        return templates.TemplateResponse(request, "_partials/taste_checkpoint.html", _checkpoint_context())

    @router.post("/taste/checkpoint/restore")
    def taste_restore_checkpoint():
        store.rec.model.restore_taste_settings(checkpoint_setting, param_keys)
        return _refresh()

    @router.get("/taste/viz/recent-sound")
    def taste_recent_sound(request: Request):
        return templates.TemplateResponse(request, "_partials/taste_recent_sound.html",
                                          {"tilt": taste_viz.centroid_tilt_panel(store, ctx.now())})

    @router.get("/taste/viz/playlist-influence")
    def taste_playlist_influence(request: Request):
        return templates.TemplateResponse(request, "_partials/taste_playlist_influence.html",
                                          {"influence": taste_viz.playlist_influence_panel(store)})

    @router.get("/taste/recall")
    def taste_recall(request: Request):
        # the expensive stats, lazy-loaded so the page paints instantly. This is the §1 model-health
        # panel: warm-path recall@k, leakage-free rolling temporal recall, cold-path projection (with
        # its failure-mode breakdown), and graduation counts by source from the §1c log.
        holdout_days = eval_recs.best_holdout(store)
        now = ctx.now()
        return templates.TemplateResponse(request, "_partials/taste_recall.html",
                                          {"recall": eval_recs.recall_at_k(store, k=20),
                                           "proj": eval_recs.projection_recall(store, k=20),
                                           "temporal": eval_recs.rolling_temporal_recall(
                                               store, holdout_days=holdout_days, k=20),
                                           "holdout_days": holdout_days,
                                           "grad_counts": store.graduation_log_counts(),
                                           "served": store.recommendation_scorecard(
                                               now - 30 * 86400, now)})

    @router.get("/taste/preview")
    def taste_preview(request: Request):
        # Manual-refresh live sample of For You under the current knobs (recomputed only on demand).
        # A random slice of the matching pool (not the top-N) so every refresh shows a new set,
        # even when the knobs haven't changed.
        items = recommend.taste_sample(store, ctx.now(), limit=8)
        return templates.TemplateResponse(request, "_partials/taste_preview.html", {
            "items": items, "lane_labels": {name: label for name, label, _ in rec_params.LANES}})

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
        weights = store.get_weights(
            now=ctx.now(),
            revert_halflife_d=rec_params.get_param(store, "weight_revert_halflife_d"))
        rows = []
        for row in current:
            old = before.get(row["key"])
            row["old_rank"] = old.get("rank") if old else None
            row["old_score"] = old.get("score") if old else None
            row["old_lane"] = old.get("lane", "") if old else ""
            row["old_lane_weight"] = old.get("lane_weight") if old else None
            if row["lane_weight"] is None and row["old_lane"]:
                # A baseline track outside today's top cohort has no current served lane, but the
                # allocation knob for the lane it occupied is still directly comparable.
                row["lane_weight"] = weights.get(f"lane:{row['old_lane']}", 1.0)
            row["status"] = ("entered" if old is None and row["in_cohort"] else
                             "exited" if old and not row["in_cohort"] else "stayed")
            row["rank_delta"] = ((row["old_rank"] - row["rank"])
                                 if row["old_rank"] and row["rank"] else None)
            row["score_delta"] = ((row["score"] - row["old_score"])
                                  if row["old_score"] is not None else None)
            row["changes"] = _trace_changes(old.get("trace", {}) if old else {}, row["trace"])
            if row["old_lane_weight"] is not None and row["lane_weight"] is not None:
                delta = row["lane_weight"] - row["old_lane_weight"]
                if abs(delta) > 1e-9:
                    row["changes"].append({"label": "Recommendation category weight", "delta": delta,
                                           "before": row["old_lane_weight"],
                                           "after": row["lane_weight"], "multiplier": True})
            row["changes"].sort(key=lambda c: -abs(c["delta"]))
            rows.append(row)
        return {"comparison": rows, "comparison_limit": 24,
                "lane_labels": {name: label for name, label, _ in rec_params.LANES}}

    def _trace_changes(old, current):
        fields = (("durable", "Saved taste match", False),
                  ("transient_delta", "Recent listening adjustment", False),
                  ("session_delta", "Session adjustment", False),
                  ("audio_delta", "Sound adjustment", False),
                  ("rank_base", "Base score after ranking", False),
                  ("axis_applied", "Combined preference multiplier", True))
        changes = []
        for key, label, multiplier in fields:
            if key not in old or key not in current:
                continue
            delta = float(current[key]) - float(old[key])
            if abs(delta) > 1e-9:
                changes.append({"label": label, "delta": delta, "before": old[key],
                                "after": current[key], "multiplier": multiplier})
        old_axes, current_axes = old.get("axes", {}), current.get("axes", {})
        for axis in sorted(set(old_axes) | set(current_axes)):
            before, after = float(old_axes.get(axis, 1.0)), float(current_axes.get(axis, 1.0))
            if abs(after - before) > 1e-9:
                changes.append({"label": f"{axis.capitalize()} multiplier", "delta": after - before,
                                "before": before, "after": after, "multiplier": True})
        return changes

    @router.post("/taste/comparison/baseline")
    def taste_comparison_baseline(request: Request):
        baseline = recommend.taste_comparison(store, ctx.now())
        compact = [{"key": row["key"], "rank": row["rank"], "score": row["score"],
                    "trace": row["trace"], "lane": row["lane"],
                    "lane_weight": row["lane_weight"]}
                   for row in baseline]
        store.set_setting(comparison_setting, json.dumps(compact))
        return templates.TemplateResponse(request, "_partials/taste_comparison.html",
                                          {**_comparison_context(), "baseline_captured": True})

    @router.get("/taste/comparison")
    def taste_comparison(request: Request):
        return templates.TemplateResponse(request, "_partials/taste_comparison.html",
                                          _comparison_context())

    @router.post("/taste/weight")
    async def taste_weight(request: Request):
        form = await request.form()
        axis, weight = form.get("axis"), form_float(form.get("weight"))
        if axis and weight is not None:
            if axis.startswith("lane:") and axis.removeprefix("lane:") not in rec_params.LANE_NAMES:
                raise HTTPException(status_code=400, detail="Unknown recommendation source")
            _save_starting_point()
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
            _save_starting_point()
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
            _save_starting_point()
            rec_params.reset_param(store, name)
        return _stale()

    @router.post("/taste/reset-all")
    def taste_reset_all():
        # Everything back to defaults: lane + genre weights and every scalar knob. Full reload so the
        # sliders visibly snap back. Leaves vectors / feedback alone (they have their own controls).
        _save_starting_point()
        store.reset_weights()
        rec_params.reset_all_params(store)
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
        run_id = store.get_setting("rec_selection_active_run")
        if not run_id:
            raise HTTPException(status_code=409, detail="There is no model selection to restore.")
        store.set_setting("rec_selection_paused", "1")
        store.set_setting("rec_selection_restore", run_id)
        ctx.rec_worker.trigger()
        return _autotune_status(request)

    @router.post("/taste/model-selection/resume")
    def resume_model_selection(request: Request):
        if store.get_setting("rec_selection_restore"):
            raise HTTPException(status_code=409, detail="Wait for the previous model to finish restoring.")
        store.set_setting("rec_selection_paused", "")
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

    @router.post("/taste/reset-weights")
    def taste_reset_weights():
        _save_starting_point()
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
