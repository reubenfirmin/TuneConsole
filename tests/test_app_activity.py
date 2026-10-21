"""Shutdown admission covers queued work and mutations that outlive their requests."""
import asyncio
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from yt_playlist.core.activity import Activity, WorkClosing
from yt_playlist.core.update_lifecycle import acquire_restart_locks
from yt_playlist.library import executor
from yt_playlist.rec.rec_worker import RecWorker
from yt_playlist.web.app import create_app
from yt_playlist.web.jobs import SyncJobs


def _ctx(store=None):
    return SimpleNamespace(store=store, work=Activity(), sync_lock=threading.Lock(),
        spotify_import_lock=threading.Lock(), jobs=SyncJobs(), now_fn=lambda: 1000,
        client_provider=lambda: {1: object()}, logger=logging.getLogger(__name__))


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


def test_restart_waits_for_a_generated_playlist_save(store, monkeypatch):
    app = create_app(store, lambda: {1: object()}, background_work=False)
    ctx = app.state.ctx
    started, release = threading.Event(), threading.Event()

    def save(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return {"new_ytm": "new", "pid": None, "added": 1}

    monkeypatch.setattr(executor, "create_generated_playlist", save)
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

    monkeypatch.setattr(executor, "gc_generated_playlists", collect)
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
