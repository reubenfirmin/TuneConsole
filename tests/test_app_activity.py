"""Shutdown admission covers queued work and mutations that outlive their requests."""
import asyncio
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from yt_playlist.core.activity import Activity, WorkClosing, check_stopping
from yt_playlist.core.update_lifecycle import acquire_restart_locks
from yt_playlist.library import playlist_generated
from yt_playlist.rec.rec_worker import RecWorker
from yt_playlist.web.app import create_app
from yt_playlist.web.jobs import SyncJobs


def _ctx(store=None):
    return SimpleNamespace(store=store, work=Activity(), sync_lock=threading.Lock(),
        spotify_import_lock=threading.Lock(), jobs=SyncJobs(), now_fn=lambda: 1000,
        client_provider=lambda: {1: object()}, logger=logging.getLogger(__name__))


@pytest.mark.parametrize("reason", ["restart", "idle"])
def test_restart_waits_for_platform_update(reason):
    ctx = _ctx()
    ctx.updater = SimpleNamespace(busy=True)
    locks, message = acquire_restart_locks(ctx, reason=reason)
    assert locks is None
    assert "update" in message
    assert not ctx.work.accepting
    assert not ctx.sync_lock.locked() and not ctx.spotify_import_lock.locked()


def test_restart_closes_admission_before_the_activity_check():
    ctx = _ctx()
    release = ctx.work.admit("Road Trip build")
    assert acquire_restart_locks(ctx)[0] is None
    assert not ctx.work.accepting
    with pytest.raises(WorkClosing):
        ctx.work.start_thread(lambda: None)
    # Already-admitted work may finish its materialization, with each child still tracked.
    with ctx.work.entered(release):
        child = ctx.work.wrap("save draft", lambda: None)
    release()
    assert not ctx.work.wait(0)
    child()
    assert ctx.work.wait(0)
    locks, _ = acquire_restart_locks(ctx)
    assert locks is not None
    for lock in locks:
        lock.release()


def test_cancel_restart_resumes_admission_without_interrupting_active_work():
    work = Activity()
    finish = work.admit("remote mutation")
    work.close_admission("restart")
    work.resume_restart()
    assert work.accepting and work.active == ("remote mutation",)
    work.close_admission("restart")
    work.close_admission("shutdown")
    work.resume_restart()
    assert not work.accepting and work.closing_reason == "shutdown"
    finish()


def test_idle_shutdown_cannot_take_over_an_accepted_restart():
    ctx = _ctx()
    ctx.work.close_admission("restart")
    assert acquire_restart_locks(ctx, reason="idle")[0] is None
    assert not ctx.sync_lock.locked() and not ctx.spotify_import_lock.locked()
    assert ctx.work.closing_reason == "restart"


@pytest.mark.parametrize("reason", ["shutdown", "idle", "update"])
def test_sync_cooperative_stop_only_interrupts_shutdown(reason):
    work = Activity()
    release = work.admit("library sync")
    try:
        with work.entered(release):
            work.close_admission(reason)
            if reason == "shutdown":
                with pytest.raises(WorkClosing):
                    check_stopping()
            else:
                check_stopping()
    finally:
        release()
    assert work.wait(0)


def test_cancelled_request_keeps_running_mutation_visible():
    work = Activity()
    started, release = threading.Event(), threading.Event()

    def mutate():
        started.set()
        assert release.wait(5)

    async def run():
        task = asyncio.create_task(work.to_thread(mutate))
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        work.close_admission()
        try:
            assert not work.wait(0)
        finally:
            release.set()
        assert await asyncio.to_thread(work.wait, 5)

    asyncio.run(run())


def test_lifespan_owns_executor_cancels_pending_work_and_supports_restart(store):
    app = create_app(store, lambda: {}, background_work=False)
    ctx = app.state.ctx
    assert ctx.work._executor is None
    assert not ctx.rec_worker._threads and ctx.enrich_worker._thread is None
    for _ in range(2):
        started, release = threading.Event(), threading.Event()
        pending_ran = []

        def mutation():
            started.set()
            assert release.wait(5)

        with TestClient(app):
            running = ctx.work.submit(mutation)
            assert started.wait(5)
            pending = ctx.work.submit(lambda: pending_ran.append(True))

            def finish_when_closing():
                while ctx.work.accepting:
                    release.wait(0.005)
                release.set()

            finisher = threading.Thread(target=finish_when_closing)
            finisher.start()
        finisher.join(5)
        assert not finisher.is_alive()
        assert running.done() and pending.cancelled() and not pending_ran
        assert ctx.work.wait(0) and ctx.work._executor is None
        assert not ctx.rec_worker._threads and ctx.enrich_worker._thread is None


def test_radio_preparation_starts_inside_lifespan_after_work_opens(store, monkeypatch):
    from yt_playlist.web import app as web_app

    started, finish = threading.Event(), threading.Event()

    class Radio:
        def __init__(self, ctx):
            self.ctx = ctx

        def retain_provenance(self):
            pass

        def prepare(self):
            def warm():
                started.set()
                assert finish.wait(5)
            self.ctx.work.start_thread(warm, name="radio-prepare")

    monkeypatch.setattr(web_app, "PlaylistRadio", Radio)
    monkeypatch.setattr(web_app, "_background_sync_loop", lambda *a, **kw: None)
    app = create_app(store, lambda: {}, background_work=True)
    ctx = app.state.ctx
    assert not started.is_set() and not ctx.work.active
    worker = SimpleNamespace(start_ticker=lambda: None, trigger_mode_refresh=lambda: None,
                             shutdown=lambda: None, join=lambda timeout: None)
    ctx.rec_worker = ctx.enrich_worker = worker
    try:
        with TestClient(app):
            assert started.wait(5)
            assert "radio-prepare" in ctx.work.active
            assert ctx.work.accepting
            finish.set()
    finally:
        finish.set()
    assert ctx.work.wait(5) and not ctx.work.accepting


def test_shutdown_cannot_restart_workers_after_deferred_startup(store, monkeypatch):
    from yt_playlist.web import app as web_app

    app = create_app(store, lambda: {}, background_work=True)
    ctx = app.state.ctx
    entered, release = threading.Event(), threading.Event()
    calls = []
    def start():
        entered.set()
        assert release.wait(5)
        calls.append('start')
    worker = SimpleNamespace(start_ticker=start, trigger_mode_refresh=lambda: None,
                             shutdown=lambda: calls.append('stop'), join=lambda timeout: None)
    ctx.rec_worker = ctx.enrich_worker = worker
    monkeypatch.setattr(ctx.radio, 'prepare', lambda: None)
    monkeypatch.setattr(web_app, '_background_sync_loop', lambda *a, **kw: None)
    observed = []
    def finish_when_closing():
        while ctx.work.accepting:
            release.wait(.005)
        observed.append(list(calls))
        release.set()
    finisher = None
    try:
        with TestClient(app):
            assert entered.wait(5)
            finisher = threading.Thread(target=finish_when_closing)
            finisher.start()
    finally:
        release.set()
        if finisher:
            finisher.join(5)
    assert observed == [[]]
    assert calls == ['start', 'start', 'stop', 'stop']


def test_restart_waits_for_a_generated_playlist_save(store, monkeypatch):
    iid = store.identities.upsert_identity("Main", "bridge", None, True)
    app = create_app(store, lambda: {iid: object()}, background_work=False)
    ctx = app.state.ctx
    started, release = threading.Event(), threading.Event()

    def save(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return {"new_ytm": "new", "pid": None, "added": 1}

    monkeypatch.setattr(playlist_generated, "create_generated_playlist", save)
    with TestClient(app, base_url="http://127.0.0.1") as client, ThreadPoolExecutor(1) as requests:
        response = requests.submit(client.post, "/home/generate",
            data={"name": "Mix", "tracks": json.dumps([{"video_id": "v"}])})
        try:
            assert started.wait(5)
            assert acquire_restart_locks(ctx)[0] is None
            assert client.post("/home/generate").status_code == 503
        finally:
            release.set()
        assert response.result(5).status_code == 200


def test_generated_playlist_gc_drains_before_restart(store, monkeypatch):
    ctx = _ctx(store)
    started, release = threading.Event(), threading.Event()

    def collect(*args):
        started.set()
        assert release.wait(5)
        return []

    monkeypatch.setattr(playlist_generated, "gc_generated_playlists", collect)
    worker = RecWorker(ctx, gc_initial_s=0)
    worker.start_ticker()
    try:
        assert started.wait(5)
        assert acquire_restart_locks(ctx)[0] is None
        worker.shutdown()
        assert not ctx.work.wait(0)
    finally:
        release.set()
        worker.join(5)
    assert ctx.work.wait(0)
