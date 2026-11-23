"""Bind Taste queries to the existing feature query and repository owners."""
from types import SimpleNamespace

from yt_playlist.controllers.taste import Taste
from yt_playlist.controllers.taste_queries import TasteQueries
from yt_playlist.rec import (album_choices, album_tuning, autotune_run, group_serving, group_viz,
    model_health, model_selection, progressive, rec_params, scorecard, surfaces, taste_analysis, taste_viz)


def taste(store):
    ranked = lambda now, *args: surfaces.taste_comparison(store, now, *args)
    changes = Taste(store.settings, store.rec_model, ranked=ranked, selection=store.model_selection)
    readings = SimpleNamespace(
        engine=lambda: taste_viz.engine_panel(store), maintenance=lambda: progressive.maintenance_view(store),
        groups_published=lambda: group_viz.prepared(store), group_view=lambda: group_serving.view(store),
        group_progress=lambda: group_serving.progress(store), group_policy=lambda: group_serving.policy(store),
        mix_choices=lambda now: taste_viz.mix_choices(store, now),
        overview=lambda now, **kw: taste_viz.overview(store, now, **kw),
        transparency=lambda now: taste_viz.model_transparency(store, now),
        source_weights=lambda now: taste_viz.source_weights(store, now),
        generated_playlists=lambda now: taste_viz.generated_playlists(store, now),
        learning=lambda now: taste_viz.learning_progress(store, now),
        selection=lambda now, **kw: model_selection.view(store, now, **kw),
        autotune=lambda: autotune_run.last_result(store), album_feedback=lambda: album_choices.feedback_state(store),
        recent_sound=lambda now: taste_viz.centroid_tilt_panel(store, now),
        playlist_influence=lambda: taste_viz.playlist_influence_panel(store),
        sample=lambda now, **kw: surfaces.taste_sample(store, now, **kw),
        health=lambda now, **kw: model_health.view(store, now, **kw),
        graduation_counts=store.rec_signals.graduation_log_counts,
        scorecard=lambda start, end: scorecard.recommendation_scorecard(store, start, end))
    queries = TasteQueries(store.rec_model, store.rec_query, store.discovery, store.rec_proposals, store.modes,
        store.rec_evidence, store.history, store.settings, readings=readings,
        parameter=lambda name: rec_params.get_param(store, name), breadth=lambda: taste_analysis.taste_breadth(store),
        ranked=ranked, tuning=lambda now: album_tuning.state(store, now))
    return changes, queries
