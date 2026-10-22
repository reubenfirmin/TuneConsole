"""Genre groups repair automatically, without sync, provider access or a full SVD rebuild."""
import logging
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from yt_playlist.rec import content_updates, embed, mode_surfaces, rec_params, rec_worker, taste_modes, taste_viz
from yt_playlist.web.app import create_app


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
    assert embed.maybe_rebuild_content_vectors(store)
    assert taste_modes.recompute(store, 1000.0) == 3
    store.history.import_play_events(iid, [(f"techno {i}|artist", f"Techno-{i}", 2000.0)
                                   for i in range(4)])
    return tracks


def _worker(store):
    return rec_worker.RecWorker(SimpleNamespace(
        store=store, now_fn=lambda: 2000.0, logger=logging.getLogger("test")), debounce_s=0)


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
        embed.build_content_and_store(store)
        assert not taste_viz.overview(store, 2000.0)["recent"]["styles_current"]
    store.settings.set_setting("enrich_worker_enabled", "0")

    def forbidden(*args, **kwargs):
        raise AssertionError("Local repair must not need a full rebuild or provider connection")

    monkeypatch.setattr(embed, "build_and_store", forbidden)
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
            assert client.get("/taste").status_code == 200
            assert not finished.is_set()
            if reason in ("stale", "missing"):
                pending = client.get("/taste/panels/overview").text
                assert 'hx-trigger="every 3s"' in pending
                assert "Updating your music groups automatically" in pending
        finally:
            release.set()
        assert finished.wait(5)
        live = taste_modes.live_modes(store)
        assert len(live) == 3
        assert {m["mode_id"] for m in live} == old_ids
        assert all(m["last_seen"] == 2000.0 for m in live)
        recent = taste_viz.overview(store, 2000.0)["recent"]
        assert recent["styles_current"] and recent["n"] == 4
        assert len(recent["shifts"]) == 3
        html = client.get("/taste/panels/overview").text
        assert "genre groups need to be updated" not in html
        assert 'hx-trigger="every 3s"' not in html
        assert store.rec_surface.get_proposals("mode_bundles") is not None


def test_same_space_content_update_places_track_without_retraining(store, library, monkeypatch):
    mode_surfaces.prepare_bundles(store, 1000.0)
    model = store.settings.get_setting("rec_content_model")
    modes = [(m["mode_id"], m["centroid"].tobytes(), m["last_seen"])
             for m in taste_modes.live_modes(store)]
    space = embed.content_space_id(store)
    tid = store.tracks.upsert_track("extra", "Zz extra", "Artist", None, None)
    store.tracks.set_track_genre(tid, "Techno")
    key = store.tracks.identity_key_for_video("extra")

    def forbidden(*args, **kwargs):
        raise AssertionError("A metadata update must reuse the saved model, centroids and rankings")

    monkeypatch.setattr(embed, "build_content_and_store", forbidden)
    monkeypatch.setattr(embed, "build_and_store", forbidden)
    monkeypatch.setattr(taste_modes, "recompute", forbidden)
    monkeypatch.setattr(mode_surfaces, "prepare_bundles", forbidden)
    assert content_updates.update_tracks(store, [key], 2000.0) == 1
    assert embed.content_space_id(store) == space
    assert store.settings.get_setting("rec_content_model") == model
    assert not taste_modes.needs_refresh(store)
    assert [(m["mode_id"], m["centroid"].tobytes(), m["last_seen"])
            for m in taste_modes.live_modes(store)] == modes
    assert key in store.rec_model.content_vectors_by_keys([key])
    bundles = store.rec_surface.get_proposals("mode_bundles")
    assert key in {row["key"] for row in bundles["all"]["temporal"]}
    memberships = [m["mode_id"] for m in taste_modes.live_modes(store)
                   if key in {row["key"] for row in bundles[str(m["mode_id"])]["temporal"]}]
    assert len(memberships) == 1


@pytest.mark.parametrize("prepared", [False, True], ids=["missing-bundles", "saved-bundles"])
def test_current_groups_are_not_recomputed_at_every_startup(store, library, monkeypatch, prepared):
    saved = None
    if prepared:
        mode_surfaces.prepare_bundles(store, 1000.0)
        saved = store.rec_surface.get_proposals("mode_bundles")

    def forbidden(*args, **kwargs):
        raise AssertionError("Startup must reuse the saved model and current groups")

    monkeypatch.setattr(embed, "maybe_rebuild_content_vectors", forbidden)
    monkeypatch.setattr(taste_modes, "recompute", forbidden)
    if prepared:
        monkeypatch.setattr(mode_surfaces, "prepare_bundles", forbidden)
    _worker(store)._do_mode_refresh()
    assert all(m["last_seen"] == 1000.0 for m in taste_modes.live_modes(store))
    bundles = store.rec_surface.get_proposals("mode_bundles")
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
        store.rec_surface.put_proposals("fresh_pool", cached, 1000)

    def forbidden(*args, **kwargs):
        raise AssertionError("Discovery repair must reuse the saved groups and model")

    monkeypatch.setattr(mode_surfaces, "prepare_bundles", forbidden)
    monkeypatch.setattr(taste_modes, "recompute", forbidden)
    monkeypatch.setattr(embed, "build_and_store", forbidden)
    _worker(store)._do_mode_refresh()
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
        worker.trigger_content_update(["song|artist"])

    monkeypatch.setattr(worker, "_do_rebuild", rebuild)
    monkeypatch.setattr(content_updates, "update_tracks",
                        lambda actual_store, keys, now: calls.append((keys, now)))
    if synchronous:
        worker.rebuild()
    else:
        worker.trigger()
    while targets:
        targets.pop(0)()
    assert calls == ["full", ({"song|artist"}, 2000.0)]
    assert not worker.busy
    assert not worker.work.active


def test_failed_full_rebuild_does_not_lose_pending_local_repair(scheduled, monkeypatch):
    worker, targets = scheduled
    calls = []

    def fail():
        raise RuntimeError("SVD failed")

    monkeypatch.setattr(worker, "_do_rebuild", fail)
    monkeypatch.setattr(worker, "_do_mode_refresh", lambda: calls.append("local"))
    monkeypatch.setattr(content_updates, "update_tracks",
                        lambda actual_store, keys, now: calls.append((keys, now)))
    worker.trigger_mode_refresh()
    worker.trigger_content_update(["song|artist"])
    worker.trigger()
    targets.pop()()
    assert calls == [({"song|artist"}, 2000.0), "local"]
    assert not worker.busy
    assert not worker.work.active
