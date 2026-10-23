"""Preference refreshes reuse the established models and update saved ranking provenance."""
import json

import pytest

from tests.rec.test_model_refresh import seed
from yt_playlist.rec import candidate_refresh, embedding_training as embed, home_prepared, model_refresh, mode_surfaces, rec_params


def refresh(store, now=2000):
    assert model_refresh.rebuild(store, now) is not None
    assert mode_surfaces.prepare_bundles(store, now) is not None


def candidate(store, key):
    pool = store.rec_proposals.get_proposals('mode_bundles') or {}
    return next((row for row in pool.get('all', {}).get('wheelhouse', []) if row['key'] == key), None)


def library(store):
    seed(store)
    refresh(store)
    pool = store.rec_proposals.get_proposals('mode_bundles')['all']['wheelhouse']
    assert pool
    return pool[0]['key']


def no_fit(*args, **kwargs):
    raise AssertionError('Preference or clock changes must reuse the completed model')


def test_genre_edits_reset_and_restore_update_scores_and_served_provenance(store, monkeypatch):
    key = library(store)
    original = candidate(store, key)
    genre = store.rec_query.track_genres([key])[key]
    from yt_playlist.util import genre_map
    axis = 'genre:' + genre_map.family(genre)
    vectors = store.rec_model.get_rec_vectors()
    monkeypatch.setattr(embed, 'build_vectors_from_baskets', no_fit)
    for weight in (0.5, 1.5, 0):
        store.rec_model.set_weight(axis, weight, lo=0, now=2000)
        assert candidate_refresh.due(store, 2000)
        refresh(store)
        row = candidate(store, key)
        if weight == 0:
            assert row is None
        else:
            assert (row['score'] < original['score']) == (weight < 1)
            policy = json.loads(store.conn.execute('SELECT configuration FROM rec_serving_policies '
                'WHERE model_version=?', (row['pool_policy'],)).fetchone()[0])
            assert policy['weights'][axis] == weight
        assert store.rec_model.get_rec_vectors() == vectors
    store.rec_model.reset_weights()
    refresh(store)
    assert candidate(store, key)['score'] == original['score']
    store.rec_model.set_weight(axis, 0.5, now=2000)
    store.rec_model.save_taste_settings('preference-checkpoint', [], 2000)
    refresh(store)
    saved_score = candidate(store, key)['score']
    store.rec_model.set_weight(axis, 1.5, now=2000)
    refresh(store)
    assert store.rec_model.restore_taste_settings('preference-checkpoint', [])
    refresh(store)
    assert candidate(store, key)['score'] == saved_score


def test_decay_and_half_life_changes_refresh_without_authoritative_edits(store, monkeypatch):
    key = library(store)
    store.rec_model.set_weight('genre:techno', 0.5, now=2000)
    refresh(store)
    before = home_prepared.tuning_key(store, 2000)
    monkeypatch.setattr(embed, 'build_vectors_from_baskets', no_fit)
    now = 2000 + 60 * 86400
    assert candidate_refresh.due(store, now)
    assert home_prepared.tuning_key(store, now) != before
    refresh(store, now)
    assert not candidate_refresh.due(store, now + 1)
    assert candidate_refresh.due(store, now + candidate_refresh.DECAY_TICK_S)
    rec_params.set_param(store, 'weight_revert_halflife_d', 30)
    assert candidate_refresh.due(store, now)
    refresh(store, now)
    assert not candidate_refresh.due(store, now + 1)


def test_stale_ranking_publication_does_not_mark_preferences_current(store, monkeypatch):
    library(store)
    store.rec_model.set_weight('genre:techno', 0.5, now=2000)
    original = mode_surfaces._calculate_bundles
    def superseded(reader, now, policies):
        result = original(reader, now, policies)
        store.rec_model.set_weight('genre:techno', 1.5, now=now)
        return result
    monkeypatch.setattr(mode_surfaces, '_calculate_bundles', superseded)
    assert mode_surfaces.prepare_bundles(store, 2000) is None
    assert candidate_refresh.due(store, 2000)
