"""Genre groups repair in the worker, without sync, provider access or request-time fitting."""
import logging
import threading
import time
from types import SimpleNamespace

import pytest

from yt_playlist.rec import (content_model, embedding_training as training, embedding_vectors as embed,
                             model_refresh, mode_surfaces, rec_params, rec_worker, taste_modes)
from tests.model_support import build_content_model, publish_modes
from tests.prepared_fixture_support import prepared_template
from tests.recommendation_support import publish_bundles


def _seed_library(store, *, per_genre=27):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    tracks = []
    for genre in ("Techno", "Jazz", "Rock"):
        for i in range(per_genre):
            title = f"{genre} {i}"
            video = f"{genre}-{i}"
            tid = store.tracks.upsert_track(video, title, "Artist", None, None)
            store.tracks.set_track_genre(tid, genre)
            tracks.append(tid)
    rec_params.set_param(store, "modes_k", 3)
    rec_params.set_param(store, "modes_min_members", 20)
    assert build_content_model(store)
    assert publish_modes(store, 1000.0) == 3
    store.history.import_play_events(iid, [(f"techno {i}|artist", f"Techno-{i}", 2000.0)
                                   for i in range(4)])
    return tracks


@pytest.fixture(scope="session")
def mode_refresh_library_template(_pristine_current_schema):
    with prepared_template(_pristine_current_schema, _seed_library) as template:
        yield template


@pytest.fixture
def library(store, mode_refresh_library_template, record_property):
    record_property('library_tracks', len(mode_refresh_library_template.result))
    return mode_refresh_library_template.restore(store)


@pytest.fixture(autouse=True)
def worker_phase_costs(monkeypatch, record_property):
    from functools import wraps

    costs, lock = {}, threading.Lock()
    def timed(name, operation):
        @wraps(operation)
        def call(*args, **kwargs):
            wall, cpu = time.monotonic(), time.thread_time()
            try:
                return operation(*args, **kwargs)
            finally:
                elapsed = time.monotonic() - wall, time.thread_time() - cpu
                with lock:
                    values = costs.setdefault(name, [0, 0.0, 0.0])
                    values[0] += 1
                    values[1] += elapsed[0]
                    values[2] += elapsed[1]
        return call

    for owner, attribute, name in (
            (rec_worker.RecWorker, '_do_mode_refresh', 'worker_repair'),
            (model_refresh, 'rebuild', 'model_refresh'),
            (mode_surfaces, 'prepare_bundles', 'bundle_preparation'),
            (mode_surfaces, '_calculate_bundles', 'ranking'),
            (mode_surfaces, '_publish_typed_bundles', 'typed_publication')):
        monkeypatch.setattr(owner, attribute, timed(name, getattr(owner, attribute)))
    yield
    with lock:
        for name, (calls, wall, cpu) in costs.items():
            record_property(name + '_calls', calls)
            record_property(name + '_wall_seconds', wall)
            record_property(name + '_thread_cpu_seconds', cpu)


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
    mode_surfaces.prepare_bundles(store, 1000.0)
    space = embed.content_space_id(store)
    tid = store.tracks.upsert_track("extra", "Zz extra", "Artist", None, None)
    store.tracks.set_track_genre(tid, "Techno")
    key = store.tracks.identity_key_for_video("extra")

    _settle(store)
    assert mode_surfaces.prepare_bundles(store, 2000) is not None
    assert embed.content_space_id(store) == space
    assert {m['mode_id'] for m in taste_modes.live_modes(store)} == {
        m['mode_id'] for m in store.modes.list_modes(active_only=True)}
    assert key in store.rec_model.content_vectors_by_keys([key])
    assert key in _pool_keys(store, 'temporal')


@pytest.mark.parametrize("prepared", [False, True], ids=["missing-bundles", "saved-bundles"])
def test_current_groups_are_not_recomputed_at_every_startup(store, library, monkeypatch, prepared):
    # Adopt this pre-ledger fixture before guarding an unchanged restart.
    _settle(store)
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
    mode_surfaces.prepare_bundles(store, 1000)
    for i in range(12):
        store.discovery.upsert_discovered_track(f"new {i}|new artist {i}", f"v{i}", f"New {i}",
            f"New artist {i}", None, None, "Jazz", "2020", "radio:seed", 1001)
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

    # Adopt this fixture's saved spaces into the production fit ledger first.
    _settle(store)
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
    return _worker(store), targets


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
    from yt_playlist.rec import trend_rollups

    worker, targets = scheduled
    calls = []
    outcomes = iter([None, {"built_at": 2000}])

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
