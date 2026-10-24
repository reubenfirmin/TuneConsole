"""Genre groups repair in the worker, without sync, provider access or request-time fitting."""
import logging
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from yt_playlist.rec import (content_model, embedding_training as training, embedding_vectors as embed,
                             model_refresh, mode_surfaces, rec_params, rec_worker, taste_modes, taste_viz)
from yt_playlist.web.app import create_app
from tests.model_support import build_content_model


@pytest.fixture
def library(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    tracks = []
    for genre in ("Techno", "Jazz", "Rock"):
        for i in range(60):
            title = f"{genre} {i}"
            video = f"{genre}-{i}"
            tid = store.tracks.upsert_track(video, title, "Artist", None, None)
            store.tracks.set_track_genre(tid, genre)
            tracks.append(tid)
    rec_params.set_param(store, "modes_k", 3)
    rec_params.set_param(store, "modes_min_members", 20)
    assert build_content_model(store)
    assert taste_modes.recompute(store, 1000.0) == 3
    store.history.import_play_events(iid, [(f"techno {i}|artist", f"Techno-{i}", 2000.0)
                                   for i in range(4)])
    return tracks


def _worker(store):
    return rec_worker.RecWorker(SimpleNamespace(
        store=store, now_fn=lambda: 2000.0, logger=logging.getLogger("test")), debounce_s=0)


def _settle(store):
    assert model_refresh.rebuild(store, 2000) is not None


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


@pytest.mark.parametrize("reason", ["stale", "missing"])
def test_startup_repairs_groups_without_sync_or_extension(store, library, monkeypatch, reason):
    old_ids = {m["mode_id"] for m in taste_modes.live_modes(store)}
    if reason == "missing":
        store.conn.execute("DELETE FROM taste_modes")
        store.conn.commit()
    else:
        # New release metadata adds a column while coverage stays in the same bucket.
        for tid in library[60:120]:
            store.tracks.set_track_enrichment(tid, "Jazz", "2001")
        build_content_model(store)
        assert not taste_viz.overview(store, 2000.0)["recent"]["styles_current"]
    store.settings.set_setting("enrich_worker_enabled", "0")

    def forbidden(*args, **kwargs):
        raise AssertionError("Requests must not fit models, and local repair must not need a provider")

    monkeypatch.setattr(rec_worker.RecWorker, "start_ticker", lambda self: None)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = rec_worker.RecWorker._do_mode_refresh

    def refresh(self, **kwargs):
        started.set()
        try:
            assert release.wait(5)
            original(self, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(rec_worker.RecWorker, "_do_mode_refresh", refresh)
    app = create_app(store, forbidden, now_fn=lambda: 2000.0, background_work=True)
    app.state.ctx.rec_worker.debounce_s = 0
    with TestClient(app, base_url="http://127.0.0.1") as client:
        try:
            # Startup and requests remain available while the repair is still waiting.
            assert started.wait(5)
            with monkeypatch.context() as request_guard:
                request_guard.setattr(training, "build_vectors_from_baskets", forbidden)
                assert client.get("/taste").status_code == 200
                assert not finished.is_set()
                pending = client.get("/taste/panels/overview").text
                assert 'hx-trigger="every 3s"' in pending
                assert "Updating your music groups automatically" in pending
        finally:
            release.set()
        assert finished.wait(5)
        deadline = time.monotonic() + 60
        while app.state.ctx.rec_worker.busy and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not app.state.ctx.rec_worker.busy
        live = taste_modes.live_modes(store)
        assert len(live) == 3
        assert {m["mode_id"] for m in live} == old_ids
        assert all(m["last_seen"] == 2000.0 for m in live)
        recent = taste_viz.overview(store, 2000.0)["recent"]
        assert recent["styles_current"] and recent["n"] == 4
        assert len(recent["shifts"]) == 3
        with monkeypatch.context() as request_guard:
            request_guard.setattr(training, "build_vectors_from_baskets", forbidden)
            html = client.get("/taste/panels/overview").text
        assert "genre groups need to be updated" not in html
        assert 'hx-trigger="every 3s"' not in html
        assert store.rec_proposals.get_proposals("mode_bundles") is not None


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
    bundles = store.rec_proposals.get_proposals("mode_bundles")
    assert key in {row["key"] for row in bundles["all"]["temporal"]}


@pytest.mark.parametrize("prepared", [False, True], ids=["missing-bundles", "saved-bundles"])
def test_current_groups_are_not_recomputed_at_every_startup(store, library, monkeypatch, prepared):
    saved = None
    if prepared:
        mode_surfaces.prepare_bundles(store, 1000.0)
        saved = store.rec_proposals.get_proposals("mode_bundles")

    def forbidden(*args, **kwargs):
        raise AssertionError("Startup must reuse the saved model and current groups")

    monkeypatch.setattr(content_model, "build_library_content", forbidden)
    monkeypatch.setattr(taste_modes, "recompute", forbidden)
    # The one-time adoption of a pre-ledger model is itself incremental. Once
    # adopted, a restart with no source changes must not learn the corpus twice.
    _settle(store)
    mode_surfaces.prepare_bundles(store, 2000)
    saved = store.rec_proposals.get_proposals('mode_bundles')
    generation = store.rec_model.content_generation()
    worker = _worker(store)
    monkeypatch.setattr(worker, '_start_loop', lambda: None)
    worker._do_mode_refresh()
    assert store.rec_model.content_generation() == generation
    bundles = store.rec_proposals.get_proposals("mode_bundles")
    assert bundles is not None
    if prepared:
        assert bundles == saved


@pytest.mark.parametrize("cached", [None, []], ids=["absent", "empty"])
def test_startup_repairs_absent_discovery_songs_while_reusing_saved_groups(store, library, monkeypatch, cached):
    from yt_playlist.rec import fresh_pool
    mode_surfaces.prepare_bundles(store, 1000)
    for i in range(12):
        store.discovery.upsert_discovered_track(f"new {i}|new artist {i}", f"v{i}", f"New {i}",
            f"New artist {i}", None, None, "Jazz", "2020", "radio:seed", 1001)
    store.conn.execute("DELETE FROM rec_proposals WHERE surface='fresh_pool'")
    store.conn.commit()
    if cached is not None:
        store.rec_proposals.put_proposals("fresh_pool", cached, 1000)

    def forbidden(*args, **kwargs):
        raise AssertionError("Discovery repair must reuse the saved groups and model")

    # Adopt this fixture's saved spaces into the production fit ledger first.
    _settle(store)
    monkeypatch.setattr(taste_modes, "recompute", forbidden)
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
        worker._retry_bundles()
    targets.pop()()
    assert calls == ["full", projection, projection]
    assert not worker.busy
    assert not worker.work.active


def test_bundle_retry_repairs_a_model_invalidated_during_preparation(store, library, monkeypatch):
    worker = _worker(store)
    prepare = mode_surfaces.prepare_bundles
    attempts = []

    def invalidate_once(reader, now):
        attempts.append(store.settings.get_setting("rec_content_model"))
        if len(attempts) == 1:
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
        worker._refresh_bundles()
    assert worker.jobs.retry['bundles'].dirty
    # The worker settles invalidated model inputs before retrying candidate pools.
    # Pool preparation must only read the model lifecycle's published result.
    _settle(store)
    monkeypatch.setattr(rec_worker.time, 'monotonic', lambda: worker.jobs.retry['bundles'].retry_at + 1)
    with monkeypatch.context() as request_guard:
        request_guard.setattr(training, "build_vectors_from_baskets", forbidden)
        worker._refresh_bundles()
    assert len(attempts) == 2 and all(attempts)
    assert store.settings.get_setting("rec_content_model")
    assert store.rec_proposals.get_proposals("mode_bundles")


def test_enrichment_requests_repair_when_saved_clusters_have_lost_their_model(store, library, monkeypatch):
    worker = _worker(store)
    calls = []
    store.settings.delete_setting("rec_content_model")
    monkeypatch.setattr(worker, "_schedule", lambda **kw: calls.append(kw))
    worker.trigger_content_update(["techno 0|artist"])
    assert calls == [{"full": True, "prepare": True}]
    assert store.rec_refresh.revision() is not None
