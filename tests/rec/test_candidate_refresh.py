"""Preference refreshes reuse the established models and update saved ranking provenance."""
from pathlib import Path
from contextlib import ExitStack, closing
from copy import deepcopy
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock, call

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
import numpy as np
import pytest

from tests.prepared_fixture_support import prepared_template, roots
from tests.recommendation_support import seed_publication_subjects
from tests.model_refresh_support import seed
from tests.rec.test_model_refresh import native_model_input_sql
from tests.store_fixture_support import (
    bounded_consumer_schema, initialize_consumer, mock_snapshot_coordinator,
    native_sql_calls, reuse_reporting_reader,
    without_query_timing,
)
from yt_playlist.core.store import Store
from yt_playlist.core import embeddings, model_contract
from yt_playlist.rec import candidate_refresh, embedding_training as embed, home_prepared, model_refresh, mode_surfaces, rec_params, surfaces
from yt_playlist.util import genre_map


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These checks build their own router without the application lifespan."""


@pytest.fixture(scope='module')
def candidate_schema():
    with bounded_consumer_schema((('house', 'House'), ('techno', 'Techno'),
                                 ('jazz', 'Jazz'), ('rock', 'Rock'))) as schema:
        yield schema


@pytest.fixture(scope='module')
def preference_schema():
    with bounded_consumer_schema((('house', 'House'), ('techno', 'Techno'),
                                 ('jazz', 'Jazz'), ('rock', 'Rock')),
                                 snapshot_guards=False) as schema:
        yield schema


@pytest.fixture
def preference_store(preference_schema, tmp_path, monkeypatch):
    """Real preference SQL/ranking/publication below snapshot coordination.

    The complete control sequences run synchronously with no competing writer.
    Captured-input races retain actual guards in the worker workflows below.
    """
    database = Store(tmp_path / 'preference-controls.db')
    with closing(database.conn):
        database.conn.execute('PRAGMA synchronous=OFF')
        initialize_consumer(database, preference_schema, snapshot_guards=False)
        database.conn.execute('PRAGMA wal_autocheckpoint=0')
        with mock_snapshot_coordinator(database, monkeypatch):
            yield database


@pytest.fixture
def store(candidate_schema, tmp_path, monkeypatch):
    database = Store(tmp_path / 'candidate-refresh.db')
    with closing(database.conn):
        # Keep real committed visibility and guarded snapshots on private WAL
        # files. These refresh owners do not claim host power-loss durability.
        database.conn.execute('PRAGMA synchronous=OFF')
        initialize_consumer(database, candidate_schema)
        database.conn.execute('PRAGMA wal_autocheckpoint=0')
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
def candidate_library_template(candidate_schema):
    """Only the initial real build; every later refresh remains a test subject."""
    with ExitStack() as resources:
        with native_model_input_sql():
            template = resources.enter_context(prepared_template(candidate_schema, library))
        yield template


def _assert_library_matches(store, original):
    assert len(store.rec_query.library_keys()) == 96
    for name in ('library_keys', 'track_content', 'track_audio_features', 'play_counts'):
        assert getattr(store.rec_query, name)() == getattr(original.rec_query, name)()
    assert store.rec_evidence.rec_baskets() == original.rec_evidence.rec_baskets()
    assert store.history.recent_plays_with_ts(limit=128) == original.history.recent_plays_with_ts(limit=128)
    for name in embeddings.SPACES:
        assert embeddings.rows(store.conn, name) == embeddings.rows(original.conn, name)
    for name in {*model_contract.FIELDS, 'rec_dim', 'rec_refresh_version', 'rec_mode_fit_inputs'} - {
            'rec_content_generation'}:
        assert store.settings.get_setting(name) == original.settings.get_setting(name)
    assert list(map(tuple, store.conn.execute('SELECT * FROM taste_modes ORDER BY mode_id'))) == list(
        map(tuple, original.conn.execute('SELECT * FROM taste_modes ORDER BY mode_id')))
    ranks = ('SELECT h.surface,m.mode_key,m.purpose,c.recording_id,c.rank,c.score,p.policy_version,'
             'b.intensity_band,b.genre_key FROM recommendation_heads h '
             'JOIN recommendation_surface_pools m ON m.generation_id=h.generation_id AND m.surface=h.surface '
             'JOIN recommendation_pools p ON p.id=m.pool_id '
             'JOIN recommendation_candidates c ON c.pool_id=p.id '
             'LEFT JOIN recommendation_radio_bands b ON b.pool_id=p.id AND b.recording_id=c.recording_id '
             'ORDER BY h.surface,m.mode_key,m.purpose,c.rank')
    assert list(map(tuple, store.conn.execute(ranks))) == list(map(tuple, original.conn.execute(ranks)))
    assert not store.rec_refresh.has_pending_changes()
    assert not candidate_refresh.due(store, 2000)
    assert store.conn.execute('PRAGMA foreign_key_check').fetchall() == []


@pytest.mark.usefixtures('native_sql_calls', 'without_query_timing')
def test_prepared_candidate_copies_match_an_independent_producer_and_keep_private_files(
        candidate_library_template, candidate_schema, tmp_path):
    # The projection-upgrade workflow originally also owned producer parity
    # and fixture isolation. Keep these actual SQL/model/file checks together;
    # completed ranking inputs in the worker checks cannot establish them.
    template = candidate_library_template
    producer = Store(tmp_path / 'producer.db')
    with closing(producer.conn):
        producer.conn.execute('PRAGMA synchronous=OFF')
        initialize_consumer(producer, candidate_schema)
        producer.conn.execute('PRAGMA wal_autocheckpoint=0')
        assert library(producer) == template.result
        with template.source._lock, producer._lock:
            _assert_library_matches(template.source, producer)
    with template.open(':memory:') as memory, template.open(tmp_path / 'copy.db') as disk:
        for current in (memory, disk):
            # Compare private, quiescent stores under their actual locks.
            # Reporting-reader admission is independent of copy equality.
            with current._lock, template.source._lock:
                _assert_library_matches(current, template.source)
                assert current.recommendations.heads() == template.source.recommendations.heads()
                assert current.rec_model.content_generation() == template.source.rec_model.content_generation()
        assert len({current._evidence_key for current in (template.source, memory, disk)}) == 3
        assert template.source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            template.source.conn.execute("INSERT INTO settings VALUES ('private_candidate_input','forbidden')")
        destinations = [roots(current.conn) for current in (memory, disk)]
        copied = 0
        for name, source in template.files.items():
            assert len({source, *(owned[name] for owned in destinations)}) == 3
            for path in source.rglob('*'):
                if path.is_file():
                    paths = [path, *(owned[name] / path.relative_to(source) for owned in destinations)]
                    assert all(private.read_bytes() == path.read_bytes() for private in paths)
                    assert len({(private.stat().st_dev, private.stat().st_ino) for private in paths}) == 3
                    copied += 1
        assert copied
        disk.rec_model.set_weight('genre:techno', 0.5, now=2000)
        assert disk.rec_model.weight_records()
        assert not memory.rec_model.weight_records() and not template.source.rec_model.weight_records()




def no_fit(*args, **kwargs):
    raise AssertionError('Preference or clock changes must reuse the completed model')


@pytest.fixture
def completed_ranking_inputs(native_sql_calls, without_query_timing, store, monkeypatch):
    """Clock admission consumes completed ranks; score changes are tested below."""
    # Keep the original complete catalog, audio, artist credits, eight baskets
    # and listening history. Only the independent fitted scores are completed
    # inputs beneath projection-version admission and publication.
    seed(store)
    items = [{'key': f'song {i:03}|artist {i}', 'video_id': f'v{i}',
              'title': f'Song {i:03}', 'artist': f'Artist {i}',
              'genre': ('Techno', 'Jazz', 'Rock')[i * 3 // 96], 'score': .8}
             for i in range(96)]
    vector = np.zeros(16, np.float32)
    vector[0] = 1
    store.rec_model.replace_rec_vectors([(item['key'], vector.tobytes()) for item in items])
    vectors = store.rec_model.get_rec_vectors()
    assert vectors
    def calculate(reader, now, policies):
        return {'all': {'wheelhouse': policies.stamp(deepcopy(items), 'wheelhouse')},
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
    key = 'song 000|artist 0'
    assert candidate(store, key)['key'] == key
    assert store.rec_model.get_rec_vectors() == completed_ranking_inputs


@pytest.mark.parametrize('projection', ['vibe', 'profile'])
def test_projection_upgrade_refreshes_legacy_pools_on_worker_without_refitting(
        store, monkeypatch, projection, completed_ranking):
    from yt_playlist.rec import home_vibe, measurement, musical_character, vibe_pools

    # Completed ranking is the lower input. Keep actual vibe preparation,
    # version derivation and native publication together across the upgrade.
    calculate = mode_surfaces._calculate_bundles

    def with_vibes(reader, now, policies):
        bundle, fresh = calculate(reader, now, policies)
        vibe_pools.prepare(reader, bundle, now, policies)
        prepared = bundle['_vibes']
        assert prepared['version'] == home_vibe.VERSION
        assert prepared['profile_version'] == musical_character.VERSION
        assert set(prepared['pools']) == set(prepared['genres']) == set(home_vibe.PRESETS)
        assert prepared['pools']['relax']['explore']
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


@pytest.fixture
def taste_parameter_inputs(monkeypatch):
    # These workflows change one scalar control and actual facet weights.
    # Supply the other registered defaults as completed lower inputs; parameter
    # persistence/clamping has its own owners. Keep real SQL reads for the
    # edited control in policy capture, due decisions and served ranking.
    defaults = {spec.name: spec.default for spec in rec_params.PARAMS}
    get_param = rec_params.get_param
    monkeypatch.setattr(rec_params, 'get_param', lambda reader, name:
        get_param(reader, name) if name == 'comfort_min_plays' else defaults[name])


def completed_policy_inputs(store, monkeypatch):
    # These two control sequences keep the clock, listening history, metadata
    # and fitted vectors unchanged. Resolve those lower inputs once, rather
    # than reconstructing them for each publication of new scalar/facet
    # preferences. Policies still capture the actual edited SQL preferences
    # and each refresh keeps its actual ranking and durable publication.
    from yt_playlist.rec import measurement, transient

    with store.read_snapshot() as snapshot:
        leans = transient.facet_leans(snapshot.store, 2000)
        models = measurement._model_identities(snapshot.store)

    def listening_inputs(reader, now):
        assert now == 2000
        return deepcopy(leans)

    monkeypatch.setattr(transient, 'facet_leans', listening_inputs)
    monkeypatch.setattr(measurement, '_model_identities', lambda reader: deepcopy(models))


@pytest.fixture
def genre_ranking(preference_store, monkeypatch, taste_parameter_inputs):
    """Native genre weighting/publication with completed lower model inputs."""
    store = preference_store
    seed_publication_subjects(store, [
        {'key': f'genre {index}|artist', 'title': f'Genre {index}', 'artist': 'Artist',
         'video_id': f'genre-{index}', 'genre': genre}
        for index, genre in enumerate(('Techno', 'Techno', 'Jazz', 'Jazz'))])
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
    completed_policy_inputs(store, monkeypatch)
    vectors = store.rec_model.get_rec_vectors()
    yield store, 'genre 0|artist', 'genre:' + genre_map.family('Techno')
    assert store.rec_model.get_rec_vectors() == vectors


@pytest.mark.usefixtures('native_sql_calls', 'without_query_timing')
def test_genre_edits_reset_and_restore_update_scores_and_served_provenance(
        genre_ranking):
    store, key, axis = genre_ranking
    assert mode_surfaces.prepare_bundles(store, 2000) is not None
    original = candidate(store, key)
    assert original is not None and len(pool(store, 'wheelhouse')) == 4
    original_pool = pool(store, 'wheelhouse')
    vectors = store.rec_model.get_rec_vectors()

    def rerank():
        assert candidate_refresh.due(store, 2000)
        assert mode_surfaces.prepare_bundles(store, 2000) is not None

    for weight in (.5, 1.5, 0):
        # This original workflow owns persisted edits, real rank changes and
        # publication provenance. The separate taste-control workflow owns
        # HTTP requests and worker scheduling across those same transitions.
        store.rec_model.set_weight(axis, weight, lo=0, now=2000)
        rerank()
        assert store.rec_model.get_weights(now=2000)[axis] == weight
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
        assert store.rec_model.get_rec_vectors() == vectors

    store.rec_model.reset_weights()
    rerank()
    assert axis not in store.rec_model.weight_records()
    assert pool(store, 'wheelhouse') == original_pool
    assert not candidate_refresh.due(store, 2000)
    store.rec_model.set_weight(axis, .5, now=2000)
    store.rec_model.save_taste_settings('taste_settings_checkpoint_v1', [], 2000)
    rerank()
    saved = pool(store, 'wheelhouse')
    saved_score = candidate(store, key)['score']
    assert len(saved) == 4
    store.rec_model.set_weight(axis, 1.5, now=2000)
    rerank()
    assert candidate(store, key)['score'] != saved_score
    assert store.rec_model.restore_taste_settings('taste_settings_checkpoint_v1', [])
    rerank()
    assert candidate(store, key)['score'] == saved_score
    assert pool(store, 'wheelhouse') == saved
    assert not candidate_refresh.due(store, 2000)
    assert store.rec_model.get_rec_vectors() == vectors


@pytest.mark.usefixtures('native_sql_calls', 'without_query_timing')
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
def small_taste_ranking(preference_store, monkeypatch, taste_parameter_inputs):
    """Real ranking controls/publications, with completed lower candidate inputs."""
    store = preference_store
    iid = store.accounts.upsert_account('Listener', 'fixture', None, True)
    seed_publication_subjects(store, [
        {'key': f'song {i}|artist {i}', 'title': f'Song {i}', 'artist': f'Artist {i}',
         'video_id': f'control-{i}', 'genre': 'Techno'} for i in range(4)])
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
    completed_policy_inputs(store, monkeypatch)
    vectors = store.rec_model.get_rec_vectors()
    yield store
    assert store.rec_model.get_rec_vectors() == vectors


@pytest.fixture
def taste_controls(preference_store, monkeypatch):
    """Exercise the real resource and worker entry point, without starting a model job."""
    from yt_playlist.rec.rec_worker import RecWorker
    from yt_playlist.web.routes import taste

    store = preference_store
    worker = RecWorker.__new__(RecWorker)
    worker._schedule = Mock()
    ctx = SimpleNamespace(store=store, rec_worker=worker, now=lambda: 2000,
        templates=Jinja2Templates(directory=str(Path(taste.__file__).parents[1] / 'templates')))
    app = FastAPI()
    # Keep the actual handlers and HTTP dispatch for this complete control
    # sequence. Unrelated panels, model selection and chart routes need no
    # dependency construction in a test that never requests them.
    from fastapi import APIRouter

    controls = {'/taste/checkpoint', '/taste/checkpoint/restore', '/taste/param',
                '/taste/reset-param', '/taste/weight', '/taste/reset-weights',
                '/taste/reset-all'}

    class ControlRoutes(APIRouter):
        def add_api_route(self, path, endpoint, **kwargs):
            if path in controls and kwargs.get('methods') == ['POST']:
                return super().add_api_route(path, endpoint, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(taste, 'APIRouter', ControlRoutes)
        router = taste.build(ctx)
    assert {(route.path, frozenset(route.methods)) for route in router.routes} == {
        (path, frozenset({'POST'})) for path in controls}
    app.include_router(router)
    with TestClient(app) as client:
        yield client, worker._schedule


@pytest.mark.usefixtures('native_sql_calls', 'without_query_timing')
def test_taste_controls_rerank_saved_pools_after_edits_resets_and_checkpoint_restore(
        small_taste_ranking, taste_controls):
    # One coherent workflow owns requests, real due decisions, worker scheduling
    # and served ranks. Completed model/candidate inputs are below this owner;
    # no request can pass merely by calling a mocked refresh predicate.
    store = small_taste_ranking
    client, schedule = taste_controls
    assert mode_surfaces.prepare_bundles(store, 2000) is not None

    def rerank(**kwargs):
        assert kwargs == {'full': False, 'bundles': True}
        assert candidate_refresh.due(store, 2000)
        assert mode_surfaces.prepare_bundles(store, 2000) is not None

    schedule.side_effect = rerank
    vectors = store.rec_model.get_rec_vectors()
    original = pool(store, 'wheelhouse')
    assert len(original) == 4
    assert [r['key'] for r in original] == [f'song {i}|artist {i}' for i in (0, 2, 1, 3)]
    original_comfort = pool(store, 'comfort')
    assert len(original_comfort) == 4
    tuning = home_prepared.tuning_key(store, 2000)
    assert client.post('/taste/checkpoint').status_code == 200
    assert client.post('/taste/param', data={'name': 'comfort_min_plays', 'value': 12}).status_code == 200
    assert rec_params.get_param(store, 'comfort_min_plays') == 12
    assert pool(store, 'comfort') == []
    assert home_prepared.tuning_key(store, 2000) != tuning
    assert client.post('/taste/reset-param', data={'name': 'comfort_min_plays'}).status_code == 200
    assert rec_params.get_param(store, 'comfort_min_plays') == 4
    assert pool(store, 'comfort') == original_comfort
    assert client.post('/taste/weight', data={'axis': 'lane:deep_cut', 'weight': 3}).status_code == 200
    assert store.rec_model.get_weights(now=2000)['lane:deep_cut'] == 3
    changed = pool(store, 'wheelhouse')
    assert [r['key'] for r in changed] == [f'song {i}|artist {i}' for i in (2, 3, 0, 1)]
    policy = store.rec_ledger.policy_configuration(changed[0]['pool_policy'])
    assert policy['weights']['lane:deep_cut'] == 3
    assert policy['params']['comfort_min_plays'] == 4
    assert client.post('/taste/reset-weights').status_code == 200
    assert 'lane:deep_cut' not in store.rec_model.weight_records()
    assert pool(store, 'wheelhouse') == original
    assert client.post('/taste/param', data={'name': 'comfort_min_plays', 'value': 12}).status_code == 200
    assert client.post('/taste/weight', data={'axis': 'lane:deep_cut', 'weight': 3}).status_code == 200
    assert client.post('/taste/checkpoint/restore').status_code == 200
    assert rec_params.get_param(store, 'comfort_min_plays') == 4
    assert store.rec_model.weight_records() == {}
    assert pool(store, 'wheelhouse') == original
    assert pool(store, 'comfort') == original_comfort
    assert client.post('/taste/param', data={'name': 'comfort_min_plays', 'value': 12}).status_code == 200
    assert client.post('/taste/reset-all').status_code == 200
    assert rec_params.get_param(store, 'comfort_min_plays') == 4
    assert store.rec_model.weight_records() == {}
    assert pool(store, 'wheelhouse') == original
    assert pool(store, 'comfort') == original_comfort
    assert schedule.call_args_list == [call(full=False, bundles=True)] * 9
    assert store.rec_model.get_rec_vectors() == vectors
    assert not candidate_refresh.due(store, 2000)


@pytest.mark.usefixtures('native_sql_calls', 'without_query_timing')
def test_lane_decay_invalidates_prepared_rankings(store, completed_ranking):
    store.rec_model.set_weight('lane:deep_cut', 3, now=2000)
    completed_ranking()
    now = 2000 + candidate_refresh.DECAY_TICK_S
    assert candidate_refresh.due(store, now)
    completed_ranking(now)
    assert not candidate_refresh.due(store, now)
