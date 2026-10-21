import logging
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from yt_playlist.web.jobs import SyncJobs
from yt_playlist.web.routes import app_update


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


@pytest.fixture
def update_app(monkeypatch):
    stopped = []
    async def stop():
        stopped.append(True)
    monkeypatch.setattr(app_update, "_stop_server", stop)
    ctx = SimpleNamespace(updater=FakeUpdater(), sync_lock=threading.Lock(),
                          spotify_import_lock=threading.Lock(), jobs=SyncJobs(),
                          logger=logging.getLogger(__name__))
    app = FastAPI()
    app.include_router(app_update.build(ctx))
    return TestClient(app, base_url="http://127.0.0.1:9123"), ctx, stopped


def test_update_controls_require_same_origin(update_app):
    client, ctx, stopped = update_app
    for origin in ("https://evil.example", "http://127.0.0.1:8123"):
        for path in ("install", "restart"):
            assert client.post(f"/app-update/{path}", headers={"Origin": origin}).status_code == 403
    assert not ctx.updater.calls
    assert not stopped
    response = client.post("/app-update/install", headers={"Origin": "http://127.0.0.1:9123"})
    assert response.json()["phase"] == "installing"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("busy", ["sync", "import", "job"])
def test_restart_waits_for_existing_work(update_app, busy):
    client, ctx, stopped = update_app
    ctx.updater.phase = "ready"
    if busy == "sync":
        ctx.sync_lock.acquire()
    elif busy == "import":
        ctx.spotify_import_lock.acquire()
    else:
        ctx.jobs.create()
    response = client.post("/app-update/restart")
    assert response.status_code == 409
    assert response.json()["busy"]
    assert not ctx.updater.calls and not stopped
    if busy != "sync":
        assert not ctx.sync_lock.locked()
    if busy != "import":
        assert not ctx.spotify_import_lock.locked()


def test_restart_launches_before_graceful_shutdown_and_cannot_repeat(update_app):
    client, ctx, stopped = update_app
    assert client.post("/app-update/restart").status_code == 409
    assert not stopped
    ctx.updater.phase = "ready"
    response = client.post("/app-update/restart")
    assert response.status_code == 202
    assert ctx.updater.calls == [("127.0.0.1", 9123)]
    assert stopped == [True]
    assert ctx.sync_lock.locked() and ctx.spotify_import_lock.locked()
    assert client.post("/app-update/restart").status_code == 202
    assert len(ctx.updater.calls) == 1 and len(stopped) == 1


def test_failed_launch_keeps_current_server_and_releases_work_locks(update_app):
    client, ctx, stopped = update_app
    ctx.updater = FakeUpdater(phase="ready", fail_restart=True)
    assert client.post("/app-update/restart").status_code == 503
    assert not stopped
    assert not ctx.sync_lock.locked() and not ctx.spotify_import_lock.locked()


def test_development_install_has_no_update_operations(update_app):
    client, ctx, stopped = update_app
    ctx.updater = None
    assert client.post("/app-update/install").status_code == 404
    assert client.post("/app-update/restart").status_code == 404
    assert not stopped
