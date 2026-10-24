import asyncio
import logging
import threading
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from yt_playlist.web.jobs import SyncJobs
from yt_playlist.web.routes import app_update
from yt_playlist.core.activity import Activity, WorkClosing


class FakeUpdater:
    def __init__(self, phase="available", fail_restart=False):
        self.phase = phase
        self.fail_restart = fail_restart
        self.calls = []

    @property
    def busy(self):
        return self.phase in {"installing", "restarting"}

    def snapshot(self):
        return {"instance": "before", "phase": self.phase, "message": "", "progress": None}

    async def status(self):
        return self.snapshot()

    async def install(self):
        self.calls.append("install")
        self.phase = "installing"
        return self.snapshot()

    async def restart(self, host, port):
        self.calls.append((host, port))
        if self.fail_restart:
            raise RuntimeError("portal spawn failed")
        self.phase = "restarting"
        return self.snapshot()

    async def close(self):
        pass


@pytest.fixture
def update_app(monkeypatch):
    stopped = []
    async def stop():
        stopped.append(True)
    monkeypatch.setattr(app_update, "_stop_server", stop)
    ctx = SimpleNamespace(updater=FakeUpdater(), sync_lock=threading.Lock(),
                          spotify_import_lock=threading.Lock(), jobs=SyncJobs(),
                          work=Activity(), logger=logging.getLogger(__name__))

    @asynccontextmanager
    async def lifespan(app):
        yield
        ctx.work.close_admission()
        await ctx.pending_restart.close()

    app = FastAPI(lifespan=lifespan)
    app.include_router(app_update.build(ctx))
    with TestClient(app, base_url="http://127.0.0.1:9123") as client:
        yield client, ctx, stopped


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline, "restart lifecycle did not finish"
        time.sleep(0.01)


def test_update_controls_require_same_origin(update_app):
    client, ctx, stopped = update_app
    for origin in ("https://evil.example", "http://127.0.0.1:8123"):
        for path in ("install", "restart", "restart/cancel"):
            assert client.post(f"/app-update/{path}", headers={"Origin": origin}).status_code == 403
    assert not ctx.updater.calls
    assert not stopped
    response = client.post("/app-update/install", headers={"Origin": "http://127.0.0.1:9123"})
    assert response.json()["phase"] == "installing"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("busy", ["sync", "import", "job", "mutation"])
def test_restart_drains_without_any_more_client_requests(update_app, busy):
    client, ctx, stopped = update_app
    ctx.updater.phase = "ready"
    if busy == "sync":
        ctx.sync_lock.acquire()
        finish = ctx.sync_lock.release
    elif busy == "import":
        ctx.spotify_import_lock.acquire()
        finish = ctx.spotify_import_lock.release
    elif busy == "mutation":
        finish = ctx.work.admit("remote playlist mutation")
    else:
        job = ctx.jobs.create()
        finish = lambda: setattr(job, "done", True)
    response = client.post("/app-update/restart")
    assert response.status_code == 202
    assert response.json()["busy"]
    assert response.json()["phase"] == "draining"
    assert not ctx.work.accepting
    assert not ctx.updater.calls and not stopped
    if busy != "sync":
        assert not ctx.sync_lock.locked()
    if busy != "import":
        assert not ctx.spotify_import_lock.locked()
    finish()
    # Navigation, page destruction, or a lost response requires no further POSTs
    # or even status reads to complete the accepted restart.
    wait_for(lambda: stopped)
    assert ctx.updater.calls == [("127.0.0.1", 9123)]


def test_restart_launches_before_graceful_shutdown_and_cannot_repeat(update_app):
    client, ctx, stopped = update_app
    assert client.post("/app-update/restart").status_code == 409
    assert not stopped
    ctx.updater.phase = "ready"
    response = client.post("/app-update/restart")
    assert response.status_code == 202
    wait_for(lambda: stopped)
    assert ctx.updater.calls == [("127.0.0.1", 9123)]
    assert stopped == [True]
    assert ctx.sync_lock.locked() and ctx.spotify_import_lock.locked()
    assert client.post("/app-update/restart").status_code == 202
    assert len(ctx.updater.calls) == 1 and len(stopped) == 1


def test_failed_launch_keeps_current_server_and_releases_work_locks(update_app):
    client, ctx, stopped = update_app
    ctx.updater = FakeUpdater(phase="ready", fail_restart=True)
    assert client.post("/app-update/restart").status_code == 202
    wait_for(lambda: ctx.pending_restart.phase is None)
    state = client.get("/app-update/status").json()
    assert state["phase"] == "ready" and "Couldn't restart" in state["message"]
    assert ctx.work.accepting
    assert not stopped
    assert not ctx.sync_lock.locked() and not ctx.spotify_import_lock.locked()
    ctx.updater.fail_restart = False
    assert client.post("/app-update/restart").status_code == 202
    wait_for(lambda: stopped)
    assert len(ctx.updater.calls) == 2


def test_manual_relaunch_keeps_server_available_and_reopens_work(update_app):
    client, ctx, stopped = update_app

    async def manual_restart(host, port):
        ctx.updater.phase = "manual_restart"
        return {**ctx.updater.snapshot(), "message": "Close TuneConsole and open it again."}

    ctx.updater.phase = "ready"
    ctx.updater.restart = manual_restart
    response = client.post("/app-update/restart")
    assert response.status_code == 202
    wait_for(lambda: ctx.pending_restart.phase is None)
    state = client.get("/app-update/status").json()
    assert state["phase"] == "manual_restart"
    assert "open it again" in state["message"]
    assert not stopped
    assert not ctx.sync_lock.locked() and not ctx.spotify_import_lock.locked()
    assert ctx.work.accepting


def test_pending_restart_is_shared_retryable_and_cancellable_while_mutation_runs(update_app):
    client, ctx, stopped = update_app
    ctx.updater.phase = "ready"
    finish = ctx.work.admit("remote playlist mutation")
    try:
        assert client.post("/app-update/restart").status_code == 202
        task = ctx.pending_restart.task
        for _ in range(3):
            assert client.post("/app-update/restart").json()["phase"] == "draining"
            state = client.get("/app-update/status").json()
            assert state["phase"] == "draining" and state["can_cancel"]
            assert client.post("/app-update/install").json()["phase"] == "draining"
        assert ctx.pending_restart.task is task
        with pytest.raises(WorkClosing):
            ctx.work.admit("new mutation")
        cancelled = client.post("/app-update/restart/cancel")
        assert cancelled.status_code == 200 and not cancelled.json()["can_cancel"]
        assert ctx.work.accepting and ctx.work.active == ("remote playlist mutation",)
        extra = ctx.work.admit("new mutation")
        extra()
        assert not ctx.updater.calls and not stopped
        # Cancellation leaves the installed update ready for a fresh request.
        assert client.post("/app-update/restart").status_code == 202
        assert ctx.pending_restart.task is not task
    finally:
        finish()
    wait_for(lambda: stopped)
    assert len(ctx.updater.calls) == 1
    assert client.post("/app-update/restart/cancel").status_code == 409


def test_invalid_restart_address_does_not_close_admission(update_app):
    client, ctx, stopped = update_app
    ctx.updater.phase = "ready"
    assert client.post("/app-update/restart", headers={"Host": "evil.example"}).status_code == 409
    assert ctx.work.accepting and ctx.pending_restart.task is None
    assert not stopped


def test_cannot_cancel_or_duplicate_a_launch_in_flight(update_app):
    client, ctx, stopped = update_app
    ctx.updater.phase = "ready"
    finish = threading.Event()

    async def launch(host, port):
        ctx.updater.calls.append((host, port))
        while not finish.is_set():
            await asyncio.sleep(0.01)
        ctx.updater.phase = "restarting"
        return ctx.updater.snapshot()

    ctx.updater.restart = launch
    try:
        assert client.post("/app-update/restart").status_code == 202
        wait_for(lambda: ctx.pending_restart.phase == "launching")
        assert client.post("/app-update/restart/cancel").status_code == 409
        assert client.post("/app-update/restart").json()["phase"] == "launching"
        assert len(ctx.updater.calls) == 1
        assert ctx.sync_lock.locked() and ctx.spotify_import_lock.locked()
    finally:
        finish.set()
    wait_for(lambda: stopped)


def test_real_application_allows_cancellation_while_other_mutations_are_blocked(update_app, store):
    from yt_playlist.web.app import create_app

    updater = FakeUpdater(phase="ready")
    app = create_app(store, lambda: {}, updater=updater, background_work=False)
    ctx = app.state.ctx
    with TestClient(app, base_url="http://127.0.0.1:9123") as client:
        finish = ctx.work.admit("playlist save")
        try:
            assert client.post("/app-update/restart").status_code == 202
            assert client.post("/app-update/install").status_code == 503
            assert client.post("/app-update/restart/cancel").status_code == 200
            assert ctx.work.active == ("playlist save",) and ctx.work.accepting
        finally:
            finish()


def test_shutdown_cancels_pending_restart_without_reopening_admission(update_app):
    client, ctx, stopped = update_app
    ctx.updater.phase = "ready"
    finish = ctx.work.admit("playlist save")
    try:
        assert client.post("/app-update/restart").status_code == 202
        ctx.work.close_admission("shutdown")
        client.portal.call(ctx.pending_restart.close)
        assert ctx.pending_restart.phase is None
        assert not ctx.work.accepting and ctx.work.active == ("playlist save",)
        assert not ctx.updater.calls and not stopped
    finally:
        finish()


def test_failed_stop_recovers_admission_without_launching_a_second_replacement(update_app):
    client, ctx, stopped = update_app
    ctx.updater.phase = "ready"

    async def fail_stop():
        raise OSError("signal failed")

    ctx.pending_restart.stop_server = fail_stop
    assert client.post("/app-update/restart").status_code == 202
    wait_for(lambda: ctx.pending_restart.phase is None)
    state = client.get("/app-update/status").json()
    assert state["phase"] == "manual_restart" and "Close TuneConsole" in state["message"]
    assert ctx.work.accepting
    assert not ctx.sync_lock.locked() and not ctx.spotify_import_lock.locked()
    assert client.post("/app-update/restart").status_code == 409
    assert len(ctx.updater.calls) == 1


def test_unexpected_drain_failure_recovers_admission_and_all_work_locks(update_app, monkeypatch):
    client, ctx, stopped = update_app
    ctx.updater.phase = "ready"

    def failed_check():
        raise RuntimeError("could not inspect jobs")

    monkeypatch.setattr(ctx.jobs, "has_active", failed_check)
    assert client.post("/app-update/restart").status_code == 202
    wait_for(lambda: ctx.pending_restart.phase is None)
    assert ctx.work.accepting
    assert not ctx.sync_lock.locked() and not ctx.spotify_import_lock.locked()
    assert not ctx.updater.calls and not stopped


def test_development_install_has_no_update_operations(update_app):
    client, ctx, stopped = update_app
    ctx.updater = None
    assert client.post("/app-update/install").status_code == 404
    assert client.post("/app-update/restart").status_code == 404
    assert not stopped
