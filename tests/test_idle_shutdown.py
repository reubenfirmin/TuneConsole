"""Packaged servers must survive browser startup, navigation, and multiple UI tabs."""
import signal
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from yt_playlist.web import app as web_app
from yt_playlist.web.jobs import SyncJobs
from yt_playlist.core.activity import Activity, WorkClosing


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(web_app, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


@pytest.mark.parametrize("initial_page", [False, True])
def test_startup_grace_survives_an_immediate_goodbye(clock, initial_page):
    presence = web_app._UIPresence()
    if initial_page:
        clock[0] = 0.6
        presence.heartbeat("startup")
        clock[0] = 0.7
        presence.goodbye("startup")
    for now in (4, 46, 119):
        clock[0] = now
        assert not presence.idle()
    clock[0] = 121
    assert presence.idle()


def test_final_close_allows_thirty_seconds_to_reopen(clock):
    presence = web_app._UIPresence()
    clock[0] = 200
    presence.heartbeat("page")
    clock[0] = 201
    presence.goodbye("page")
    clock[0] = 230
    assert not presence.idle()
    clock[0] = 232
    assert presence.idle()


def test_delayed_goodbye_cannot_close_the_replacement_page(clock):
    presence = web_app._UIPresence()
    clock[0] = 200
    presence.heartbeat("old")
    clock[0] = 210
    presence.heartbeat("new")
    clock[0] = 211
    presence.goodbye("old")
    clock[0] = 242
    assert not presence.idle()
    presence.goodbye("new")
    clock[0] = 273
    assert presence.idle()


def test_closing_one_tab_keeps_a_throttled_background_tab_alive(clock):
    presence = web_app._UIPresence()
    clock[0] = 200
    presence.heartbeat("background")
    presence.heartbeat("foreground")
    clock[0] = 201
    presence.goodbye("foreground")
    clock[0] = 260
    assert not presence.idle()
    presence.heartbeat("background")
    clock[0] = 499
    assert not presence.idle()
    clock[0] = 561
    assert presence.idle()


def test_pageshow_renews_a_cached_document_after_goodbye(clock):
    presence = web_app._UIPresence()
    clock[0] = 200
    presence.heartbeat("cached")
    presence.goodbye("cached")
    clock[0] = 220
    presence.heartbeat("cached")
    clock[0] = 251
    assert not presence.idle()


def test_legacy_and_launcher_heartbeats_tolerate_anonymous_goodbyes(clock):
    presence = web_app._UIPresence()
    clock[0] = 200
    presence.heartbeat("")
    clock[0] = 201
    presence.goodbye("")
    presence.goodbye("unknown")
    clock[0] = 350
    assert not presence.idle()
    clock[0] = 501
    assert presence.idle()


@pytest.fixture
def idle_app(monkeypatch, clock):
    monkeypatch.setenv("YT_PLAYLIST_EXIT_ON_IDLE", "1")
    app = FastAPI()
    templates = SimpleNamespace(env=SimpleNamespace(globals={}))
    monitor = web_app._install_idle_shutdown(app, templates)
    assert templates.env.globals["exit_on_idle"]
    def wait(seconds):
        web_app.time.sleep(seconds)
        return False
    stop = SimpleNamespace(wait=wait, is_set=lambda: False)
    return app, lambda: monitor(stop)


def test_routes_keep_page_leases_separate_and_monitor_eventually_stops(idle_app, clock, monkeypatch):
    app, monitor = idle_app
    client = TestClient(app)
    clock[0] = 200
    assert client.post("/heartbeat?page=one").status_code == 204
    assert client.post("/heartbeat?page=two").status_code == 204
    assert client.post("/goodbye?page=one").status_code == 204
    clock[0] = 240
    kills = []

    def sleep(_):
        assert not kills
        if clock[0] == 240:
            assert client.post("/goodbye?page=two").status_code == 204
        clock[0] += 10

    web_app.time.sleep = sleep
    monkeypatch.setattr(web_app.os, "kill", lambda pid, sig: kills.append((clock[0], sig)))
    monitor()
    assert kills == [(270, signal.SIGTERM)]


@pytest.mark.parametrize("work", ["update", "spotify_import", "library_sync"])
def test_monitor_waits_for_background_work(idle_app, clock, monkeypatch, work):
    app, monitor = idle_app
    ctx = SimpleNamespace(updater=SimpleNamespace(busy=work == "update"),
                          spotify_import_lock=threading.Lock(), sync_lock=threading.Lock(),
                          jobs=SyncJobs())
    if work == "spotify_import":
        ctx.spotify_import_lock.acquire()
    if work == "library_sync":
        ctx.sync_lock.acquire()
    app.state.ctx = ctx
    clock[0] = 200
    kills = []

    def sleep(_):
        assert not kills
        clock[0] += 1
        if clock[0] == 203:
            ctx.updater.busy = False
            if ctx.spotify_import_lock.locked():
                ctx.spotify_import_lock.release()
            if ctx.sync_lock.locked():
                ctx.sync_lock.release()
        if clock[0] == 202:
            assert not ctx.work.accepting

    web_app.time.sleep = sleep
    monkeypatch.setattr(web_app.os, "kill", lambda pid, sig: kills.append((clock[0], sig)))
    monitor()
    assert kills == [(203, signal.SIGTERM)]


@pytest.mark.parametrize("kind", ["playlist_enrichment", "album_enrichment", "fresh_pool", "periodic_worker"])
def test_last_tab_close_drains_owned_work_then_exits(idle_app, clock, monkeypatch, kind):
    app, monitor = idle_app
    from yt_playlist.web import jobs
    ctx = SimpleNamespace(updater=None, work=Activity(), sync_lock=threading.Lock(),
        spotify_import_lock=threading.Lock(), jobs=SyncJobs(), store=SimpleNamespace(),
        logger=SimpleNamespace(warning=lambda *a, **kw: None))
    app.state.ctx = ctx
    started, finish = threading.Event(), threading.Event()

    def blocking(*args, **kwargs):
        started.set()
        assert finish.wait(5)

    if kind in ("playlist_enrichment", "album_enrichment"):
        ctx.store.tracks.tracks_for_waterfall = lambda pid: []
        monkeypatch.setattr(jobs.enrichment, "load_config", lambda store: {})
        monkeypatch.setattr(jobs.waterfall, "run_waterfall", blocking)
        job = jobs.start_playlist_enrichment(ctx, 1 if kind == "playlist_enrichment" else 2)
    elif kind == "fresh_pool":
        from yt_playlist.rec import fresh_pool
        monkeypatch.setattr(fresh_pool, "populate", blocking)
        monkeypatch.setattr(fresh_pool, "prepare", lambda *a: None)
        ctx.now_fn = lambda: 1000
        job = jobs.start_fresh_pool(ctx, {})
    else:
        # Worker loops themselves are idle; only a currently executing batch owns activity.
        ctx.work.start_thread(blocking, name="enrichment batch")
        job = None
    assert started.wait(5)
    clock[0] = 200
    kills = []

    def sleep(_):
        clock[0] += 1
        if clock[0] == 202:
            assert not kills and not ctx.work.accepting
            with pytest.raises(WorkClosing):
                ctx.work.admit("replacement work")
        if clock[0] == 203:
            finish.set()
            assert ctx.work.wait(5)
            if job:
                assert job.done

    web_app.time.sleep = sleep
    monkeypatch.setattr(web_app.os, "kill", lambda pid, sig: kills.append((clock[0], sig)))
    try:
        monitor()
        assert kills == [(203, signal.SIGTERM)]
    finally:
        finish.set()
        assert ctx.work.wait(5)


def test_returning_ui_cancels_idle_drain(idle_app, clock, monkeypatch):
    app, monitor = idle_app
    ctx = SimpleNamespace(updater=None, work=Activity(), sync_lock=threading.Lock(),
        spotify_import_lock=threading.Lock(), jobs=SyncJobs())
    app.state.ctx = ctx
    release = ctx.work.admit("pool build")
    clock[0] = 200
    client, kills = TestClient(app), []

    def sleep(_):
        clock[0] += 1
        if clock[0] == 202:
            assert not ctx.work.accepting
            assert client.post("/heartbeat?page=returned").status_code == 204
            assert ctx.work.accepting
            release()
        elif clock[0] == 203:
            assert not kills
            client.post("/goodbye?page=returned")

    web_app.time.sleep = sleep
    monkeypatch.setattr(web_app.os, "kill", lambda pid, sig: kills.append((clock[0], sig)))
    monitor()
    assert kills == [(233, signal.SIGTERM)]


def test_development_server_has_no_idle_shutdown(monkeypatch):
    monkeypatch.delenv("YT_PLAYLIST_EXIT_ON_IDLE", raising=False)
    app = FastAPI()
    templates = SimpleNamespace(env=SimpleNamespace(globals={}))
    web_app._install_idle_shutdown(app, templates)
    assert not templates.env.globals["exit_on_idle"]
    assert TestClient(app).post("/heartbeat").status_code == 404
