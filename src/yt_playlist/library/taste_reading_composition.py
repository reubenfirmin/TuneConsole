"""Bind repository and existing model-analysis capabilities to Taste reading commands."""
from types import SimpleNamespace as NS

from yt_playlist.controllers.taste_readings import TasteReadings
from yt_playlist.library import listening_composition as layers
from yt_playlist.rec import (embedding_training, embedding_vectors, group_viz, mix_tuning,
    mode_eval, mode_profiles, rec_params, scoring, surfaces, taste_analysis, transient)
from yt_playlist.rec.eligibility import home_exclusions


def readings(store):
    q = NS(**{name: getattr(store, attribute, None) for name, attribute in (
        ('model', 'rec_model'), ('signals', 'rec_signals'), ('modes', 'modes'), ('history', 'history'),
        ('charts', 'charts'), ('settings', 'settings'), ('query', 'rec_query'), ('player_events', 'player_events'),
        ('proposals', 'rec_proposals'), ('recommendations', 'recommendations'))})
    a = NS(group_prepared=lambda: group_viz.prepared(store),
        group_choices=lambda now, preview: group_viz.choices(store, now, preview),
        group_layers=lambda now, preview: group_viz.mode_layers(store, now, preview),
        choice_evidence=lambda now: mode_eval.choice_evidence(store, now),
        mode_profiles=lambda: mode_profiles.describe_modes(store),
        playlist_taste=lambda: scoring.playlist_taste(store), home_exclusions=lambda keys, now: home_exclusions(store, keys, now),
        breadth=lambda: taste_analysis.taste_breadth(store), era_shares=lambda: taste_analysis.era_distribution(store),
        facet_leans=lambda now: transient.facet_leans(store, now), radio_ids=lambda: transient.radio_list_ids(store),
        evidence_plays=lambda limit: transient.evidence_plays(store, limit),
        recent=lambda now, **kw: layers.recent_evidence(store, now, **kw),
        now_mix=lambda now, **kw: layers.now_mode_mix(store, now, **kw),
        session_mix=lambda now, **kw: layers.session_mode_mix(store, now, **kw),
        coverage=lambda now, **kw: layers.evidence_coverage(store, now, **kw),
        vectors=lambda: embedding_vectors.load_vectors(store),
        session_tilt=lambda now, matrix, index: layers.session_tilt(store, now, matrix, index),
        transient_tilt=lambda now, matrix, index: transient.centroid_tilt(store, now, matrix, index),
        axes=lambda labels: store.genres.feature_axes(labels))
    configuration = NS(lanes=rec_params.LANES, lane_default=rec_params.LANE_DEFAULT, lane_max=rec_params.LANE_MAX,
        training_dim=embedding_training.DIM, catalog_content_percent=round(surfaces.CATALOG_CONTENT_FRAC * 100))
    return TasteReadings(queries=q, analysis=a, parameter=lambda name: rec_params.get_param(store, name),
        configuration=configuration, files=getattr(store, '_recommendation_files', False))


def read(store, operation, *args, **kwargs):
    if operation in ('source_weights', 'learning_progress', 'model_transparency'):
        mix_tuning.expire(store, args[0] if args else kwargs['now'])
    if operation in ('overview', 'model_transparency', 'playlist_influence_panel', 'centroid_tilt_panel', '_stored_examples'):
        if not getattr(store, '_read_only', False) and not store._lock.held_by_current_thread:
            with store.read_view() as reader:
                return getattr(readings(reader), operation)(*args, **kwargs)
    return getattr(readings(store), operation)(*args, **kwargs)
