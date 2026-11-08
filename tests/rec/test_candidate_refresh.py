"""Preference refreshes reuse the established models and update saved ranking provenance."""
from pathlib import Path
from contextlib import closing
import time
from types import SimpleNamespace
from unittest.mock import Mock, call

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
import numpy as np
import pytest

from tests.conftest import seed_catalog_track
from tests.prepared_fixture_support import prepared_template
from tests.recommendation_support import seed_publication_subjects
from tests.candidate_timing_support import candidate_costs, candidate_prefix_costs
from tests.rec.test_model_refresh import refresh_costs, seed
from tests.store_fixture_support import bounded_consumer_schema, initialize_consumer, reuse_reporting_reader
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.core.store import Store
from yt_playlist.rec import candidate_refresh, embedding_training as embed, home_prepared, model_refresh, mode_surfaces, rec_params, surfaces
from yt_playlist.util import genre_map


@pytest.fixture(scope='module')
def candidate_schema():
    with bounded_consumer_schema((('house', 'House'), ('techno', 'Techno'), ('jazz', 'Jazz'))) as schema:
        yield schema


@pytest.fixture
def store(candidate_schema, tmp_path, monkeypatch):
    database = Store(tmp_path / 'candidate-refresh.db')
    with closing(database.conn):
        initialize_consumer(database, candidate_schema)
        database.conn.execute('PRAGMA synchronous=NORMAL')
        # Reporting reads consume the current private database. Preparation
        # keeps its actual pinned snapshots and guarded publication.
        with reuse_reporting_reader(database, monkeypatch):
            yield database


@pytest.fixture(autouse=True)
def completed_retention_maintenance(monkeypatch):
    # Refresh admission, ranking and publication stay real. Collection of
    # retired policy files belongs to the independent retention checks.
    from yt_playlist.rec import measurement

    monkeypatch.setattr(measurement, 'maintain_storage', lambda *args, **kwargs: True)


def refresh(store, now=2000):
    assert model_refresh.rebuild(store, now) is not None
    assert mode_surfaces.prepare_bundles(store, now) is not None


def candidate(store, key):
    return next((row for row in pool(store, 'wheelhouse') if row['key'] == key), None)


def pool(store, lane):
    return store.recommendations.page('home', 'all', lane)


def library(store):
    seed(store)
    refresh(store)
    candidates = pool(store, 'wheelhouse')
    assert candidates
    return candidates[0]['key']


@pytest.fixture(scope='module')
def candidate_library_template(_pristine_current_schema, candidate_prefix_costs):
    """Only the initial real build; every later refresh remains a test subject."""
    started = time.perf_counter()
    with prepared_template(_pristine_current_schema, library) as template:
        candidate_prefix_costs['candidate_cold_template_seconds'] = time.perf_counter() - started
        yield template




def no_fit(*args, **kwargs):
    raise AssertionError('Preference or clock changes must reuse the completed model')


@pytest.fixture
def completed_ranking_inputs(store, monkeypatch):
    """Clock admission consumes completed ranks; score changes are tested below."""
    key = 'completed candidate|artist'
    item = {'key': key, 'video_id': 'completed-candidate', 'title': 'Completed candidate',
            'artist': 'Artist', 'genre': 'Techno', 'score': .8}
    seed_publication_subjects(store, [item])
    store.rec_model.replace_rec_vectors([(key, np.array([1., 0.], np.float32).tobytes())])
    vectors = store.rec_model.get_rec_vectors()
    assert vectors
    def calculate(reader, now, policies):
        return {'all': {'wheelhouse': policies.stamp([dict(item)], 'wheelhouse')},
                '_meta': {'full_pools': True, 'comfort_pool': 0}}, None

    monkeypatch.setattr(mode_surfaces, '_calculate_bundles', calculate)
    monkeypatch.setattr(embed, 'build_vectors_from_baskets', no_fit)
    return vectors


@pytest.fixture
def completed_ranking(store, completed_ranking_inputs):
    def publish(now=2000):
        assert mode_surfaces.prepare_bundles(store, now) is not None

    yield publish
    # Check the final durable output once; the tests exercise every intermediate
    # deadline, and no_fit rejects model work during any publication.
    key = 'completed candidate|artist'
    assert candidate(store, key)['key'] == key
    assert store.rec_model.get_rec_vectors() == completed_ranking_inputs


@pytest.mark.parametrize('projection', ['vibe', 'profile'])
def test_projection_preparation_uses_current_versions_without_refitting(
        store, monkeypatch, projection, completed_ranking_inputs):
    from yt_playlist.rec import home_vibe, measurement, musical_character, vibe_pools

    module = home_vibe if projection == 'vibe' else musical_character
    monkeypatch.setattr(module, 'VERSION', module.VERSION + '-upgrade')
    monkeypatch.setattr(measurement, 'serving_policy',
                        lambda *args, **kwargs: ('projection-fixture', {'fixture': 'projection-upgrade'}))
    with store.read_snapshot() as snapshot:
        policies = measurement.PoolPolicies(snapshot.store, 2000)
        bundle, _fresh = mode_surfaces._calculate_bundles(snapshot.store, 2000, policies)
        vibe_pools.prepare(snapshot.store, bundle, 2000, policies)
    prepared = bundle['_vibes']
    assert prepared['version'] == home_vibe.VERSION
    assert prepared['profile_version'] == musical_character.VERSION
    assert set(prepared['pools']) == set(prepared['genres']) == set(home_vibe.PRESETS)
    assert prepared['pools']['relax']['explore']
    assert store.rec_model.get_rec_vectors() == completed_ranking_inputs
    assert not store.recommendations.heads()


@pytest.mark.parametrize('projection', ['vibe', 'profile'])
def test_projection_upgrade_refreshes_legacy_pools_on_worker_without_refitting(
        store, monkeypatch, projection, completed_ranking):
    from yt_playlist.rec import home_vibe, measurement, musical_character

    # Completed preparation is the lower input. The independent check above
    # owns its version derivation; this check owns actual worker admission,
    # framing and native publication on both sides of the upgrade.
    calculate = mode_surfaces._calculate_bundles

    def with_vibes(reader, now, policies):
        bundle, fresh = calculate(reader, now, policies)
        items = bundle['all']['wheelhouse']
        bundle['_vibes'] = {
            'version': home_vibe.VERSION, 'profile_version': musical_character.VERSION,
            'pools': {preset: {'explore': policies.stamp(items, 'vibe:' + preset)}
                      for preset in home_vibe.PRESETS}, 'genres': {}}
        return bundle, fresh

    monkeypatch.setattr(mode_surfaces, '_calculate_bundles', with_vibes)
    # Fingerprint construction and retired-policy collection have separate
    # owners; version-driven worker admission still uses its real preferences.
    monkeypatch.setattr(measurement, 'serving_policy',
                        lambda *args, **kwargs: ('projection-fixture', {'fixture': 'projection-upgrade'}))
    monkeypatch.setattr(measurement, 'maintain_storage', lambda *args, **kwargs: True)
    completed_ranking()
    vectors = store.rec_model.get_rec_vectors()
    previous = store.recommendations.head('home')['id']
    assert not candidate_refresh.due(store, 2000)

    module = home_vibe if projection == 'vibe' else musical_character
    monkeypatch.setattr(module, 'VERSION', module.VERSION + '-upgrade')
    assert candidate_refresh.due(store, 2000)

    completed_ranking()
    assert store.recommendations.head('home')['id'] != previous
    refreshed = store.recommendations.framing('home')
    assert refreshed['vibe_version'] == home_vibe.VERSION
    assert refreshed['vibe_profile_version'] == musical_character.VERSION
    assert {row['surface'] for row in store.recommendations.heads()
            if row['surface'].startswith('vibe:')} == {'vibe:' + key for key in home_vibe.PRESETS}
    assert store.recommendations.page('vibe:relax', '', 'explore')
    assert not candidate_refresh.due(store, 2001)
    assert store.rec_model.get_rec_vectors() == vectors


def test_genre_mute_http_accepts_zero_and_schedules_candidate_work(store, taste_controls):
    client, schedule = taste_controls
    axis = 'genre:' + genre_map.family('Techno')
    # Genre controls permit zero (hard mute), unlike the ordinary lane floor.
    assert client.post('/taste/weight', data={'axis': axis, 'weight': 0}).status_code == 200
    assert store.rec_model.get_weights(now=2000)[axis] == 0
    assert client.post('/taste/reset-weights').status_code == 200
    assert axis not in store.rec_model.weight_records()
    assert schedule.call_args_list == [call(full=False, bundles=True)] * 2


@pytest.fixture
def genre_ranking(store, monkeypatch):
    """Native genre weighting/publication with completed lower model inputs."""
    with atomic_write(store):
        for index, genre in enumerate(('Techno', 'Techno', 'Jazz', 'Jazz')):
            seed_catalog_track(store, f'genre {index}|artist', f'Genre {index}', 'Artist',
                               video_id=f'genre-{index}', genre=genre)
    store.rec_model.replace_rec_vectors([
        (f'genre {i}|artist', np.array([1., i / 4], np.float32).tobytes()) for i in range(4)])
    # The fitted preference geometry is an input. Keep the actual vector read,
    # facet weighting, muted eligibility, ranker, policy capture and publication.
    taste = SimpleNamespace(allocated_scores=lambda matrix: np.full(len(matrix), .5))
    monkeypatch.setattr(surfaces, 'playlist_taste', lambda *a, **kw: taste)
    monkeypatch.setattr(type(store.rec_candidates), 'deep_cuts', lambda self, **kw: [])
    monkeypatch.setattr(embed, 'build_vectors_from_baskets', no_fit)

    def calculate(reader, now, policies):
        ranked = [mode_surfaces._item_dict(item) for item in surfaces.for_you(reader, now, limit=4)]
        return {'all': {'wheelhouse': policies.stamp(ranked, 'wheelhouse')},
                '_meta': {'full_pools': True}}, None

    monkeypatch.setattr(mode_surfaces, '_calculate_bundles', calculate)
    vectors = store.rec_model.get_rec_vectors()
    yield store, 'genre 0|artist', 'genre:' + genre_map.family('Techno')
    assert store.rec_model.get_rec_vectors() == vectors


@pytest.mark.parametrize('weight', [.5, 1.5, 0])
def test_genre_weight_reranks_saved_candidates_and_retains_policy(genre_ranking, weight):
    store, key, axis = genre_ranking
    assert mode_surfaces.prepare_bundles(store, 2000) is not None
    original = candidate(store, key)
    assert original is not None and len(pool(store, 'wheelhouse')) == 4
    store.rec_model.set_weight(axis, weight, lo=0, now=2000)
    assert candidate_refresh.due(store, 2000)
    assert mode_surfaces.prepare_bundles(store, 2000) is not None
    row = candidate(store, key)
    if weight == 0:
        assert row is None
        assert [item['key'] for item in pool(store, 'wheelhouse')] == ['genre 2|artist', 'genre 3|artist']
    else:
        assert (row['score'] < original['score']) == (weight < 1)
    head = store.recommendations.head('home')
    policy = store.rec_ledger.policy_configuration(head['policy_version'])
    assert policy['weights'][axis] == weight
    if row is not None:
        assert store.rec_ledger.policy_configuration(row['pool_policy'])['weights'][axis] == weight
    assert not candidate_refresh.due(store, 2000)


@pytest.mark.parametrize('restore', ['reset', 'checkpoint'])
def test_genre_weight_restoration_republishes_exact_saved_scores(genre_ranking, restore):
    store, key, axis = genre_ranking
    if restore == 'checkpoint':
        store.rec_model.set_weight(axis, .5, now=2000)
        store.rec_model.save_taste_settings('taste_settings_checkpoint_v1', [], 2000)
    assert mode_surfaces.prepare_bundles(store, 2000) is not None
    original = pool(store, 'wheelhouse')
    assert candidate(store, key) is not None and len(original) == 4
    store.rec_model.set_weight(axis, 1.5, now=2000)
    assert mode_surfaces.prepare_bundles(store, 2000) is not None
    assert candidate(store, key)['score'] != next(row['score'] for row in original if row['key'] == key)
    if restore == 'checkpoint':
        assert store.rec_model.restore_taste_settings('taste_settings_checkpoint_v1', [])
    else:
        store.rec_model.reset_weights()
    assert candidate_refresh.due(store, 2000)
    assert mode_surfaces.prepare_bundles(store, 2000) is not None
    assert pool(store, 'wheelhouse') == original
    assert not candidate_refresh.due(store, 2000)


@pytest.mark.parametrize('change', ['elapsed-time', 'half-life'])
def test_decay_and_half_life_changes_refresh_without_authoritative_edits(
        store, completed_ranking, change):
    store.rec_model.set_weight('genre:techno', 0.5, now=2000)
    now = 2000 + 60 * 86400
    prepared_at = 2000 if change == 'elapsed-time' else now
    completed_ranking(prepared_at)
    assert not candidate_refresh.due(store, prepared_at)
    before = home_prepared.tuning_key(store, prepared_at)
    if change == 'half-life':
        rec_params.set_param(store, 'weight_revert_halflife_d', 30)
    assert home_prepared.tuning_key(store, now) != before
    assert candidate_refresh.due(store, now)
    completed_ranking(now)
    assert not candidate_refresh.due(store, now + 1)
    assert candidate_refresh.due(store, now + candidate_refresh.DECAY_TICK_S)


def test_completed_ranking_retains_captured_preferences_and_leaves_later_edits_due(
        store, monkeypatch, completed_ranking):
    completed_ranking()
    store.rec_model.set_weight('genre:techno', 0.5, now=2000)
    original = mode_surfaces._calculate_bundles
    def superseded(reader, now, policies):
        result = original(reader, now, policies)
        store.rec_model.set_weight('genre:techno', 1.5, now=now)
        return result
    monkeypatch.setattr(mode_surfaces, '_calculate_bundles', superseded)
    before = store.recommendations.head('home')['id']
    assert mode_surfaces.prepare_bundles(store, 2000) is not None
    assert store.recommendations.head('home')['id'] != before
    policy = store.rec_ledger.policy_configuration(store.recommendations.head('home')['policy_version'])
    assert policy['weights']['genre:techno'] == .5
    assert candidate_refresh.due(store, 2000)


@pytest.fixture
def small_taste_ranking(store, monkeypatch):
    """Real ranking controls/publications, with completed lower candidate inputs."""
    iid = store.accounts.upsert_account('Listener', 'fixture', None, True)
    with atomic_write(store):
        for i in range(4):
            seed_catalog_track(store, f'song {i}|artist {i}', f'Song {i}', f'Artist {i}',
                               video_id=f'control-{i}', genre='Techno')
    keys = [store.tracks.identity_key_for_video(f'control-{i}') for i in range(4)]
    store.history.import_play_events(iid, [
        (key, f'control-{i}', 1000 - day * 86400)
        for i, key in enumerate(keys) for day in range(1, 6)])
    store.rec_model.replace_rec_vectors([
        (key, np.array([1., i / 4], np.float32).tobytes()) for i, key in enumerate(keys)])
    metadata = store.rec_query.tracks_by_keys(keys)
    rows = [{**metadata[key], 'key': key, 'plays': 0, 'shared_playlists': 1} for key in keys]
    monkeypatch.setattr(surfaces, 'playlist_taste', lambda *a, **kw: None)
    # Preparation opens a separate snapshot-bound controller. Supply completed
    # candidate inputs there too, while retaining real ranking and publication.
    monkeypatch.setattr(type(store.rec_candidates), 'more_like_rotation',
                        lambda self, **kw: [dict(row) for row in rows[:2]])
    monkeypatch.setattr(type(store.rec_candidates), 'deep_cuts',
                        lambda self, **kw: [dict(row) for row in rows[2:]])

    def calculate(reader, now, policies):
        # Keep the actual fair queue, SQL comfort threshold, policy capture and
        # typed publication; unrelated model/mode/Vibe preparation is complete.
        lanes = {'wheelhouse': surfaces.for_you(reader, now, limit=4),
                 'comfort': surfaces.comfort_listening(reader, now, limit=4)}
        return {'all': {lane: policies.stamp([mode_surfaces._item_dict(item) for item in items], lane)
                        for lane, items in lanes.items()},
                '_meta': {'full_pools': True, 'comfort_pool': len(lanes['comfort'])}}, None

    monkeypatch.setattr(mode_surfaces, '_calculate_bundles', calculate)
    monkeypatch.setattr(embed, 'build_vectors_from_baskets', no_fit)
    vectors = store.rec_model.get_rec_vectors()
    yield store
    assert store.rec_model.get_rec_vectors() == vectors


@pytest.fixture
def taste_controls(store, monkeypatch):
    """Exercise the real resource and worker entry point, without starting a model job."""
    from yt_playlist.rec.rec_worker import RecWorker
    from yt_playlist.web.routes import taste

    worker = RecWorker.__new__(RecWorker)
    worker._schedule = Mock()
    monkeypatch.setattr(candidate_refresh, 'due', Mock(return_value=True))
    ctx = SimpleNamespace(store=store, rec_worker=worker, now=lambda: 2000,
        templates=Jinja2Templates(directory=str(Path(taste.__file__).parents[1] / 'templates')))
    app = FastAPI()
    app.include_router(taste.build(ctx))
    with TestClient(app) as client:
        yield client, worker._schedule


def test_taste_parameter_edit_and_reset_queue_candidate_work(store, taste_controls):
    client, schedule = taste_controls
    assert client.post('/taste/param', data={'name': 'comfort_min_plays', 'value': 12}).status_code == 200
    assert rec_params.get_param(store, 'comfort_min_plays') == 12
    assert client.post('/taste/reset-param', data={'name': 'comfort_min_plays'}).status_code == 200
    assert rec_params.get_param(store, 'comfort_min_plays') == 4
    assert schedule.call_args_list == [call(full=False, bundles=True)] * 2


def test_taste_weight_edit_and_reset_queue_candidate_work(store, taste_controls):
    client, schedule = taste_controls
    assert client.post('/taste/weight', data={'axis': 'lane:deep_cut', 'weight': 3}).status_code == 200
    assert store.rec_model.get_weights(now=2000)['lane:deep_cut'] == 3
    assert client.post('/taste/reset-weights').status_code == 200
    assert 'lane:deep_cut' not in store.rec_model.weight_records()
    assert schedule.call_args_list == [call(full=False, bundles=True)] * 2


def test_taste_checkpoint_restore_and_reset_all_queue_candidate_work(store, taste_controls):
    client, schedule = taste_controls
    seed_catalog_track(store, 'retained|artist', 'Retained', 'Artist', video_id='checkpoint-retained')
    store.rec_model.replace_rec_vectors([
        ('retained|artist', np.array([1., 0.], np.float32).tobytes())])
    vectors = store.rec_model.get_rec_vectors()
    assert client.post('/taste/checkpoint').status_code == 200
    assert client.post('/taste/param', data={'name': 'comfort_min_plays', 'value': 12}).status_code == 200
    assert client.post('/taste/weight', data={'axis': 'lane:deep_cut', 'weight': 3}).status_code == 200
    assert client.post('/taste/checkpoint/restore').status_code == 200
    assert rec_params.get_param(store, 'comfort_min_plays') == 4
    assert store.rec_model.weight_records() == {}
    assert client.post('/taste/param', data={'name': 'comfort_min_plays', 'value': 12}).status_code == 200
    assert client.post('/taste/reset-all').status_code == 200
    assert rec_params.get_param(store, 'comfort_min_plays') == 4
    assert store.rec_model.weight_records() == {}
    assert store.rec_model.get_rec_vectors() == vectors
    assert schedule.call_args_list == [call(full=False, bundles=True)] * 5


def test_comfort_parameter_reranks_real_saved_pools_and_updates_tuning(small_taste_ranking):
    store = small_taste_ranking
    assert mode_surfaces.prepare_bundles(store, 2000) is not None
    original = pool(store, 'comfort')
    assert len(original) == 4
    tuning = home_prepared.tuning_key(store, 2000)
    rec_params.set_param(store, 'comfort_min_plays', 12)
    assert candidate_refresh.due(store, 2000)
    assert mode_surfaces.prepare_bundles(store, 2000) is not None
    assert pool(store, 'comfort') == []
    assert home_prepared.tuning_key(store, 2000) != tuning
    rec_params.reset_param(store, 'comfort_min_plays')
    assert mode_surfaces.prepare_bundles(store, 2000) is not None
    assert pool(store, 'comfort') == original
    assert not candidate_refresh.due(store, 2000)


@pytest.mark.parametrize('restore', ['reset', 'checkpoint'])
def test_lane_weight_reranks_real_saved_pools_and_retains_policy(small_taste_ranking, restore):
    store = small_taste_ranking
    if restore == 'checkpoint':
        store.rec_model.save_taste_settings('taste_settings_checkpoint_v1', [], 2000)
    assert mode_surfaces.prepare_bundles(store, 2000) is not None
    original = pool(store, 'wheelhouse')
    assert len(original) == 4
    assert [r['key'] for r in original] == [f'song {i}|artist {i}' for i in (0, 2, 1, 3)]
    store.rec_model.set_weight('lane:deep_cut', 3, now=2000)
    assert candidate_refresh.due(store, 2000)
    assert mode_surfaces.prepare_bundles(store, 2000) is not None
    changed = pool(store, 'wheelhouse')
    assert [r['key'] for r in changed] == [f'song {i}|artist {i}' for i in (2, 3, 0, 1)]
    policy = store.rec_ledger.policy_configuration(changed[0]['pool_policy'])
    assert policy['weights']['lane:deep_cut'] == 3
    assert policy['params']['comfort_min_plays'] == 4
    if restore == 'checkpoint':
        assert store.rec_model.restore_taste_settings('taste_settings_checkpoint_v1', [])
    else:
        store.rec_model.reset_weights()
    assert mode_surfaces.prepare_bundles(store, 2000) is not None
    assert pool(store, 'wheelhouse') == original
    assert not candidate_refresh.due(store, 2000)


def test_lane_decay_invalidates_prepared_rankings(store, completed_ranking):
    store.rec_model.set_weight('lane:deep_cut', 3, now=2000)
    completed_ranking()
    now = 2000 + candidate_refresh.DECAY_TICK_S
    assert candidate_refresh.due(store, now)
    completed_ranking(now)
    assert not candidate_refresh.due(store, now)
