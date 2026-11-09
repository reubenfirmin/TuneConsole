"""Genre groups repair in the worker, without sync, provider access or request-time fitting."""
import logging
import threading
from types import SimpleNamespace

import pytest

from yt_playlist.rec import (content_model, embedding_training as training, embedding_vectors as embed, measurement,
                             model_refresh, mode_surfaces, rec_params, rec_worker, taste_modes)
from tests.recommendation_support import publish_bundles, seed_publication_subjects
from tests.store_fixture_support import (
    bounded_consumer_schema, initialize_consumer, mock_snapshot_coordinator, native_sql_calls, without_query_timing,
)
from yt_playlist.core import schema_inspection
from yt_playlist.core.store import Store


pytestmark = pytest.mark.usefixtures('native_sql_calls', 'without_query_timing')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These workers run directly without constructing the HTTP application."""


def _seed_library(store, *, per_genre=27):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    # These workers consume already saved clusters. Clustering discovery and
    # its corpus threshold belong to test_taste_modes_discover; model refresh,
    # saved-vector reads and publication remain real here.
    seed_publication_subjects(store, [
        {'key': f'{genre.lower()} {i}|artist', 'video_id': f'{genre}-{i}',
         'title': f'{genre} {i}', 'artist': 'Artist', 'genre': genre}
        for genre in ('Techno', 'Jazz', 'Rock') for i in range(per_genre)])
    tracks = [row[0] for row in store.conn.execute('SELECT id FROM recordings ORDER BY id')]
    rec_params.set_param(store, "modes_k", 3)
    rec_params.set_param(store, "modes_min_members", 20)
    store.history.import_play_events(iid, [(f"techno {i}|artist", f"Techno-{i}", 2000.0)
                                   for i in range(4)])
    assert model_refresh.rebuild(store, 1000.0) is not None
    _, vectors, index = embed.load_content_vectors(store)
    store.modes.replace_modes([
        {'mode_id': mid, 'label': genre, 'families': [[genre.lower(), 1]],
         'centroid': vectors[index[f'{genre.lower()} 0|artist']].copy(),
         'space': embed.content_space_id(store), 'size': per_genre,
         'rep_keys': [f'{genre.lower()} 0|artist']}
        for mid, genre in enumerate(('Techno', 'Jazz', 'Rock'), 1)], [], 1000.)
    assert len(store.modes.list_modes(active_only=True)) == 3
    return tracks


@pytest.fixture(scope='module')
def worker_schema():
    with bounded_consumer_schema(entries=(('techno', 'Techno'), ('jazz', 'Jazz'), ('rock', 'Rock')),
                                 snapshot_guards=False) as schema:
        yield schema


@pytest.fixture
def store(tmp_path, worker_schema, monkeypatch):
    database = Store(tmp_path / 'worker.db')
    try:
        database.conn.execute('PRAGMA synchronous=OFF')
        initialize_consumer(database, worker_schema, snapshot_guards=False)
        database.conn.execute('PRAGMA wal_autocheckpoint=0')
        # These tests own worker scheduling and saved model/candidate repair.
        # Concurrent snapshot validation has its own tests; model fitting,
        # real SQL and typed publication remain live beneath this collaborator.
        with mock_snapshot_coordinator(database, monkeypatch), schema_inspection.reuse_tables(database.conn):
            yield database
    finally:
        database.conn.close()


@pytest.fixture
def library(store, request, record_property):
    tracks = _seed_library(store, per_genre=getattr(request, 'param', 27))
    record_property('library_tracks', len(tracks))
    return tracks


def _worker(store):
    return rec_worker.RecWorker(SimpleNamespace(
        store=store, now_fn=lambda: 2000.0, logger=logging.getLogger("test")), debounce_s=0)


def _settle(store):
    assert model_refresh.rebuild(store, 2000) is not None


def _pool_keys(store, lane):
    head = store.recommendations.head('home')
    assert head is not None
    after, keys = -1, []
    while True:
        page = store.recommendations.candidate_page('home', 'all', lane,
            generation=head['id'], after=after, limit=128)
        keys.extend(track['key'] for track in page['tracks'])
        if page['exhausted']:
            return keys
        after = page['after']


def test_missing_pools_are_scheduled_even_while_models_need_refresh(store, monkeypatch):
    worker = _worker(store)
    worker.jobs.retry[rec_worker.Job.FULL].dirty = True
    scheduled = []
    monkeypatch.setattr(worker, '_schedule', lambda **kwargs: scheduled.append(kwargs))
    worker._poll_refresh()
    assert {'full': True, 'prepare': True} in scheduled
    assert {'full': False, 'bundles': True} in scheduled


def test_deferred_model_fit_still_prepares_candidates(store, monkeypatch):
    worker = _worker(store)
    prepared = []
    monkeypatch.setattr(model_refresh, 'rebuild', lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, '_refresh_bundles', lambda: prepared.append(True))
    worker._rebuild()
    assert prepared == [True]


def test_content_update_refreshes_original_models_and_places_track(store, library, monkeypatch):
    # The update must replace an existing publication. Its prior ranking is a
    # completed input; retain all 81 subjects there, then exercise real model
    # refresh and ranking after the extra track arrives.
    existing = [{'key': key, **track} for key, track in
                store.rec_query.tracks_by_keys(store.rec_query.library_keys()).items()]
    with monkeypatch.context() as initial:
        initial.setattr(mode_surfaces, '_calculate_bundles', lambda reader, now, policies:
            ({'all': {'temporal': policies.stamp(existing, 'temporal')}}, []))
        mode_surfaces.prepare_bundles(store, 1000.0)
    space = embed.content_space_id(store)
    # Metadata ingestion is below repair of the model and candidate pools.
    seed_publication_subjects(store, [{'key': 'zz extra|artist', 'video_id': 'extra',
        'title': 'Zz extra', 'artist': 'Artist', 'genre': 'Techno'}])
    key = store.tracks.identity_key_for_video("extra")

    _settle(store)
    # This assertion owns placement from the refreshed content vectors. Supply
    # completed non-temporal scorer pools for every library subject; temporal
    # membership, nearest-mode assignment and typed publication remain actual.
    ranked = [SimpleNamespace(key=key, **track) for key, track in
              store.rec_query.tracks_by_keys(store.rec_query.library_keys()).items()]
    monkeypatch.setattr(mode_surfaces, '_rank_pools', lambda *args:
                        {'wheelhouse': ranked, 'explore': ranked, 'comfort': ranked, 'fresh': []})
    assert mode_surfaces.prepare_bundles(store, 2000) is not None
    assert embed.content_space_id(store) == space
    assert {m['mode_id'] for m in taste_modes.live_modes(store)} == {
        m['mode_id'] for m in store.modes.list_modes(active_only=True)}
    assert key in store.rec_model.content_vectors_by_keys([key])
    assert key in _pool_keys(store, 'temporal')


@pytest.mark.parametrize("prepared", [False, True], ids=["missing-bundles", "saved-bundles"])
def test_current_groups_are_not_recomputed_at_every_startup(store, library, monkeypatch, prepared):
    # Verify the completed input is settled before guarding an unchanged restart.
    _settle(store)
    # Startup consumes completed ranking inputs; candidate placement has its
    # own real-ranking owner above. Keep publication and model reuse live.
    items = [{'key': f'techno {i}|artist', 'video_id': f'Techno-{i}',
              'title': f'Techno {i}', 'artist': 'Artist'} for i in range(4)]
    def ranked(reader, now, policies):
        return {'all': {'temporal': policies.stamp(items, 'temporal')}}, []
    monkeypatch.setattr(mode_surfaces, '_calculate_bundles', ranked)
    saved = None
    if prepared:
        mode_surfaces.prepare_bundles(store, 1000.0)
        saved = _pool_keys(store, 'temporal')
    else:
        assert store.recommendations.head('home') is None

    def forbidden(*args, **kwargs):
        raise AssertionError("Startup must reuse the saved model and current groups")

    monkeypatch.setattr(content_model, "build_library_content", forbidden)
    monkeypatch.setattr(taste_modes, "prepare_modes", forbidden)
    generation = store.rec_model.content_generation()
    groups = store.modes.list_modes()
    worker = _worker(store)
    monkeypatch.setattr(worker, '_start_loop', lambda: None)
    worker._do_mode_refresh()
    assert store.rec_model.content_generation() == generation
    assert store.recommendations.head('home') is not None
    assert [m['mode_id'] for m in store.modes.list_modes()] == [m['mode_id'] for m in groups]
    if prepared:
        assert _pool_keys(store, 'temporal') == saved


@pytest.mark.parametrize("cached", [None, []], ids=["absent", "empty"])
def test_startup_repairs_absent_discovery_songs_while_reusing_saved_groups(store, library, monkeypatch, cached):
    from yt_playlist.rec import fresh_pool
    # The initial publication supplies completed input for the startup repair.
    # Discovery encoding and the subsequent worker's ranking remain real.
    with monkeypatch.context() as initial:
        initial.setattr(mode_surfaces, '_calculate_bundles', lambda *args: ({}, []))
        initial.setattr(measurement, 'serving_policy', lambda *args, **kwargs:
                        ('startup-input', {'schema': 4, 'algorithm': 'startup-input'}))
        mode_surfaces.prepare_bundles(store, 1000)
    discoveries = [{'key': f'new {i}|new artist {i}', 'video_id': f'v{i}',
        'title': f'New {i}', 'artist': f'New artist {i}', 'genre': 'Jazz', 'year': '2020'}
        for i in range(12)]
    seed_publication_subjects(store, discoveries,
        discoveries=[track['key'] for track in discoveries], now=1001)
    if cached is None:
        with store.conn:
            store.conn.execute("DELETE FROM recommendation_heads WHERE surface='fresh'")
        assert store.recommendations.head('fresh') is None
    else:
        publish_bundles(store, {}, 1000, fresh=cached)
        empty = store.recommendations.candidate_page('fresh', 'all', 'fresh', limit=1)
        assert empty['tracks'] == [] and empty['exhausted']

    def forbidden(*args, **kwargs):
        raise AssertionError("Discovery repair must reuse the saved groups and model")

    # Complete discovery encoding before guarding reuse of the saved groups.
    _settle(store)
    # Only discovery repair is under test here. Supply completed library
    # rankings for all 81 original subjects; keep discovery scoring live.
    from yt_playlist.rec import surfaces
    library_pool = [SimpleNamespace(key=key, **track) for key, track in
                    store.rec_query.tracks_by_keys(store.rec_query.library_keys()).items()]
    for name in ('for_you', 'explore_for_you', 'comfort_listening'):
        monkeypatch.setattr(surfaces, name, lambda *args, **kwargs: library_pool)
    monkeypatch.setattr(taste_modes, "prepare_modes", forbidden)
    monkeypatch.setattr(training, "build_vectors_from_baskets", forbidden)
    worker = _worker(store)
    monkeypatch.setattr(worker, '_start_loop', lambda: None)
    worker._do_mode_refresh()
    assert len(fresh_pool.pick(store, 2001)) == 12


@pytest.fixture
def scheduled(store, monkeypatch):
    targets = []

    class QueuedThread:
        def __init__(self, *, target, daemon):
            self.target = target

        def start(self):
            targets.append(self.target)

    monkeypatch.setattr(rec_worker, "threading", SimpleNamespace(
        Lock=threading.Lock, RLock=threading.RLock, Event=threading.Event, Thread=QueuedThread))
    worker = _worker(store)
    # The queue tests exercise ordering and retries. Downstream home/story
    # preparation is a separate collaborator, just like the full rebuild below.
    monkeypatch.setattr(worker.tasks, 'home', lambda: False)
    monkeypatch.setattr(worker.tasks, 'stories', lambda: False)
    return worker, targets


def test_full_rebuild_absorbs_a_burst_of_pending_local_refreshes(scheduled, monkeypatch):
    worker, targets = scheduled
    calls = []
    monkeypatch.setattr(worker, "_do_rebuild", lambda: calls.append("full"))
    monkeypatch.setattr(worker, "_do_mode_refresh", lambda **kw: calls.append("local"))
    worker.trigger_mode_refresh()
    worker.trigger_mode_refresh()
    worker.trigger()
    assert len(targets) == 1
    targets.pop()()
    assert calls == ["full"]
    assert not worker.busy


@pytest.mark.parametrize("synchronous", [False, True])
def test_metadata_update_during_rebuild_gets_a_local_catchup(scheduled, monkeypatch, synchronous):
    worker, targets = scheduled
    calls = []

    def rebuild():
        calls.append("full")
        if len(calls) == 1:
            worker.trigger_content_update(["song|artist"])
        else:
            assert worker.ctx.store.rec_refresh.revision() is not None
            with worker.ctx.store.conn:
                worker.ctx.store.conn.execute('DELETE FROM rec_change_queue')

    monkeypatch.setattr(worker, "_do_rebuild", rebuild)
    if synchronous:
        worker.rebuild()
    else:
        worker.trigger()
    while targets:
        targets.pop(0)()
    assert calls == ["full", "full"]
    assert not worker.busy
    assert not worker.work.active


def test_failed_full_rebuild_does_not_lose_pending_local_repair(scheduled, monkeypatch):
    worker, targets = scheduled
    calls = []

    def fail():
        raise RuntimeError("SVD failed")

    monkeypatch.setattr(worker, "_do_rebuild", fail)
    monkeypatch.setattr(worker, "_do_mode_refresh", lambda: calls.append("local"))
    worker.trigger_mode_refresh()
    worker.trigger_content_update(["song|artist"])
    worker.trigger()
    targets.pop()()
    assert calls == ["local"]
    assert worker.ctx.store.rec_refresh.revision() is not None
    assert not worker.busy
    assert not worker.work.active


@pytest.mark.parametrize("projection", ["trends", "bundles"])
def test_stale_projection_retry_does_not_refit_models_or_repeat_other_stages(scheduled, monkeypatch, projection):
    from yt_playlist.rec import maintenance, trend_rollups

    worker, targets = scheduled
    calls = []
    outcomes = iter([None, {"built_at": 2000}])
    # Source fingerprint capture is below the retry scheduler being tested.
    monkeypatch.setattr(maintenance, 'inputs', lambda *args: (1, False))
    monkeypatch.setattr(maintenance, 'input_fingerprints',
                        lambda *args: {'trends': (1, False)})

    def build(store, now):
        calls.append(projection)
        return next(outcomes)

    monkeypatch.setattr(trend_rollups if projection == "trends" else mode_surfaces,
                        "build" if projection == "trends" else "prepare_bundles", build)
    refresh = worker._refresh_trends if projection == "trends" else worker._refresh_bundles
    monkeypatch.setattr(worker, "_do_rebuild", lambda: (calls.append("full"), refresh()))
    worker.trigger()
    targets.pop()()
    assert calls == ['full', projection]
    if projection == 'trends':
        monkeypatch.setattr(rec_worker.time, 'monotonic', lambda: worker.projections.retry_at['trends'] + 1)
        worker.projections.poll()
    else:
        assert worker.jobs.retry['bundles'].dirty
        worker._retry_bundles()
        assert not targets
        monkeypatch.setattr(rec_worker.time, 'monotonic', lambda: worker.jobs.retry['bundles'].retry_at + 1)
        from yt_playlist.rec import candidate_refresh
        worker.ctx.now_fn = lambda: 2000 + candidate_refresh.CHECK_INTERVAL_S
        worker._retry_bundles()
    targets.pop()()
    assert calls == ["full", projection, projection]
    assert not worker.busy
    assert not worker.work.active


@pytest.mark.parametrize('failure', ['model-invalidated', 'policy-capacity', 'unexpected'])
def test_bundle_retry_repairs_a_model_invalidated_during_preparation(store, library, monkeypatch, caplog, failure):
    from yt_playlist.rec import candidate_refresh
    worker = _worker(store)
    prepare = mode_surfaces.prepare_bundles
    attempts = []
    clock = [100.0]
    wall = [2000.0]
    worker.ctx.now_fn = lambda: wall[0]
    if failure == 'policy-capacity':
        monkeypatch.setattr(rec_worker, 'time', SimpleNamespace(monotonic=lambda: clock[0]))

    def invalidate_once(reader, now):
        attempts.append(store.settings.get_setting("rec_content_model"))
        if len(attempts) == 1:
            if failure == 'policy-capacity':
                from yt_playlist.core.policy_config import PolicyCapacityFull
                # The real-library ranking outlasts the start-to-start interval.
                clock[0] += 90
                raise PolicyCapacityFull(131000, 60000000, 1024, 500000)
            if failure == 'unexpected':
                raise RuntimeError('Unexpected ranking failure')
            # A new, ambiguous upload rekeys catalog subjects and invalidates derived models.
            from yt_playlist.core.catalog import invalidate_derived
            with store.conn:
                invalidate_derived(store.conn)
            return None
        return prepare(reader, now)

    def forbidden(*args, **kwargs):
        pytest.fail("Recovering prepared pools must not fit the collaborative model")

    monkeypatch.setattr(mode_surfaces, "prepare_bundles", invalidate_once)
    monkeypatch.setattr(worker, "_start_loop", lambda: None)
    _settle(store)
    with monkeypatch.context() as request_guard:
        request_guard.setattr(training, "build_vectors_from_baskets", forbidden)
        with caplog.at_level(logging.INFO):
            worker._refresh_bundles()
    assert worker.jobs.retry['bundles'].dirty
    if failure == 'policy-capacity':
        assert worker.jobs.retry['bundles'].failures == 0
        assert worker.jobs.retry['bundles'].deferrals == 1
        assert 'batch adds 1024 variants' in caplog.text
        assert not any(record.exc_info for record in caplog.records)
        assert worker.jobs.retry['bundles'].retry_at >= clock[0] + candidate_refresh.CHECK_INTERVAL_S
    elif failure == 'unexpected':
        failures = [record for record in caplog.records if 'rec bundle preparation failed' in record.message]
        assert len(failures) == 1 and failures[0].exc_info[0] is RuntimeError
    # The worker settles invalidated model inputs before retrying candidate pools.
    # Pool preparation must only read the model lifecycle's published result.
    _settle(store)
    if failure == 'policy-capacity':
        clock[0] = worker.jobs.retry['bundles'].retry_at - .001
        worker._refresh_bundles()
        worker._retry_bundles()
        assert len(attempts) == 1 and not worker.jobs.has('bundles')
    monkeypatch.setattr(rec_worker.time, 'monotonic', lambda: worker.jobs.retry['bundles'].retry_at + 1)
    wall[0] += candidate_refresh.CHECK_INTERVAL_S
    with monkeypatch.context() as request_guard:
        request_guard.setattr(training, "build_vectors_from_baskets", forbidden)
        worker._refresh_bundles()
    assert len(attempts) == 2 and all(attempts)
    assert store.settings.get_setting("rec_content_model")
    assert store.recommendations.head('home') is not None
    assert _pool_keys(store, 'temporal')


def test_enrichment_requests_repair_when_saved_clusters_have_lost_their_model(store, library, monkeypatch):
    worker = _worker(store)
    calls = []
    store.settings.delete_setting("rec_content_model")
    monkeypatch.setattr(worker, "_schedule", lambda **kw: calls.append(kw))
    worker.trigger_content_update(["techno 0|artist"])
    assert calls == [{"full": True, "prepare": True}]
    assert store.rec_refresh.revision() is not None
