"""Genre groups repair automatically, without sync, provider access or a full SVD rebuild."""
import logging
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from yt_playlist.rec import embed, rec_params, rec_worker, taste_modes, taste_viz
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


@pytest.mark.parametrize("reason", ["stale", "missing", "catalog"])
def test_startup_repairs_groups_without_sync_or_extension(store, library, monkeypatch, reason):
    old_ids = {m["mode_id"] for m in taste_modes.live_modes(store)}
    if reason == "missing":
        store.conn.execute("DELETE FROM taste_modes")
        store.conn.commit()
    else:
        # New release metadata adds a column while coverage stays in the same bucket.
        for tid in library[60:120]:
            store.tracks.set_track_enrichment(tid, "Jazz", "2001")
        if reason == "stale":
            embed.build_content_and_store(store)
            assert not taste_viz.overview(store, 2000.0)["recent"]["styles_current"]
        else:
            store.settings.set_setting("rec_content_genre_revision", "older-catalog")
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


def test_same_space_content_refresh_updates_group_membership(store, library):
    space = embed.content_space_id(store)
    tid = store.tracks.upsert_track("extra", "Zz extra", "Artist", None, None)
    store.tracks.set_track_genre(tid, "Techno")
    embed.build_content_and_store(store)
    assert embed.content_space_id(store) == space
    assert not taste_modes.needs_refresh(store)
    _worker(store)._do_mode_refresh(force=True)
    assert sum(m["size"] for m in taste_modes.live_modes(store)) == 181


def test_current_groups_are_not_recomputed_at_every_startup(store, library):
    _worker(store)._do_mode_refresh()
    assert all(m["last_seen"] == 1000.0 for m in taste_modes.live_modes(store))
    assert store.rec_surface.get_proposals("mode_bundles") is None


@pytest.fixture
def scheduled(store, monkeypatch):
    targets = []

    class QueuedThread:
        def __init__(self, *, target, daemon):
            self.target = target

        def start(self):
            targets.append(self.target)

    monkeypatch.setattr(rec_worker, "threading", SimpleNamespace(
        Lock=threading.Lock, Event=threading.Event, Thread=QueuedThread))
    return _worker(store), targets


def test_full_rebuild_absorbs_a_burst_of_pending_local_refreshes(scheduled, monkeypatch):
    worker, targets = scheduled
    calls = []
    monkeypatch.setattr(worker, "_do_rebuild", lambda: calls.append("full"))
    monkeypatch.setattr(worker, "_do_mode_refresh", lambda **kw: calls.append("local"))
    worker.trigger_mode_refresh()
    worker.trigger_mode_refresh(force=True)
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
        worker.trigger_mode_refresh(force=True)

    monkeypatch.setattr(worker, "_do_rebuild", rebuild)
    monkeypatch.setattr(worker, "_do_mode_refresh", lambda **kw: calls.append(kw))
    if synchronous:
        worker.rebuild()
    else:
        worker.trigger()
    while targets:
        targets.pop(0)()
    assert calls == ["full", {"force": True}]
    assert not worker.busy


def test_failed_full_rebuild_does_not_lose_pending_local_repair(scheduled, monkeypatch):
    worker, targets = scheduled
    calls = []

    def fail():
        raise RuntimeError("SVD failed")

    monkeypatch.setattr(worker, "_do_rebuild", fail)
    monkeypatch.setattr(worker, "_do_mode_refresh", lambda **kw: calls.append(kw))
    worker.trigger_mode_refresh(force=True)
    worker.trigger()
    targets.pop()()
    assert calls == [{"force": True}]
    assert not worker.busy
