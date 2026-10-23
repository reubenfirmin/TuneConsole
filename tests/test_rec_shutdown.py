"""Quit interrupts disposable calculations without abandoning admitted mutations."""
import logging
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from yt_playlist.core.activity import Activity, WorkClosing
from yt_playlist.rec import embedding_training as embed, model_refresh, ppr
from yt_playlist.rec.rec_worker import RecWorker


# Deliberately unbounded for the test's lifetime; the progress callback must stop it.
SLOW_SQL = """
WITH RECURSIVE slow(n) AS (
    VALUES(query_started()) UNION ALL SELECT n+1 FROM slow WHERE n<1000000000
)
SELECT sum(n) FROM slow
"""


@pytest.mark.parametrize("stop_worker", [False, True])
@pytest.mark.parametrize("operation", ["first-play", "content-snapshot"])
def test_quit_interrupts_rollup_sql_rolls_back_and_releases_waiting_request(
        store, monkeypatch, caplog, stop_worker, operation):
    work = Activity()
    caplog.set_level(logging.INFO, logger=__name__)
    worker = RecWorker(SimpleNamespace(store=store, work=work,
        logger=logging.getLogger(__name__), now_fn=lambda: 1), debounce_s=0)
    store.trends.upsert_first_play_min([("track", "saved", 1, 86400, "saved")])
    entered, request_waiting = threading.Event(), threading.Event()
    store.conn.create_function("query_started", 0, lambda: (entered.set(), 0)[1])

    # Exercise the real first-play refresh savepoint: cancellation must retain its
    # previously published data, even if SQLite rolls back the savepoint itself.
    if operation == "first-play":
        monkeypatch.setattr(store.trends, "_rebuild_artist_first_play",
                            lambda: store.conn.execute(SLOW_SQL).fetchone())
        calculate = store.trends.refresh_first_play
    else:
        monkeypatch.setattr(store.rec_model, "get_rec_content_vectors",
                            lambda: store.conn.execute(SLOW_SQL).fetchone())
        calculate = store.rec_model.content_generation
    monkeypatch.setattr(worker, "_rebuild",
                        lambda: worker._stage("database calculation", calculate))

    def waiting_request():
        request_waiting.set()
        return store.trends.first_play_map("track")

    with ThreadPoolExecutor(1) as requests:
        worker.trigger()
        try:
            assert entered.wait(5)
            response = requests.submit(waiting_request)
            assert request_waiting.wait(5)
            assert not response.done()
            if stop_worker:
                worker.shutdown()
            else:
                # First Ctrl-C closes admission before Uvicorn begins lifespan cleanup.
                work.close_admission()
            worker.join(3)
            assert response.result(3) == {"saved": 1}
            assert work.wait(0) and not worker.busy
            assert not store.conn.in_transaction
            assert "rec worker rebuild failed" not in caplog.text
            assert "rec worker stopped for shutdown" in caplog.text
        finally:
            # Keep a broken regression from leaving test-owned threads alive.
            work.close_admission()
            worker.shutdown()
            store.conn.interrupt()
            worker.join(5)


def test_cancelled_rebuild_does_not_continue_after_best_effort_stage(store, monkeypatch):
    from yt_playlist.rec import mode_surfaces, model_refresh

    work = Activity()
    worker = RecWorker(SimpleNamespace(store=store, work=work,
        logger=logging.getLogger(__name__), now_fn=lambda: 1))
    monkeypatch.setattr(model_refresh, "rebuild", lambda *args, **kwargs: {})

    def cancel(*args, **kwargs):
        work.close_admission()
        raise WorkClosing("stopped")

    monkeypatch.setattr(mode_surfaces, "prepare_bundles", cancel)

    def unexpected(*args, **kwargs):
        pytest.fail("A cancelled rebuild started another stage")

    monkeypatch.setattr(worker, "trigger_home_refresh", unexpected)
    with pytest.raises(WorkClosing):
        worker.rebuild()
    assert work.wait(0) and not worker.busy


def test_shutdown_sql_cancellation_does_not_interrupt_an_admitted_mutation(store):
    work = Activity()
    release = work.admit("save playlist")
    try:
        with work.entered(release):
            work.close_admission()
            # Long enough to invoke the same SQLite progress handler many times.
            total = store.conn.execute("WITH RECURSIVE numbers(n) AS (VALUES(0) UNION ALL "
                "SELECT n+1 FROM numbers WHERE n<10000) SELECT sum(n) FROM numbers").fetchone()[0]
            store.settings.set_setting("saved-after-quit", str(total))
    finally:
        release()
    assert store.settings.get_setting("saved-after-quit") == "50005000"
    assert work.wait(0)


def test_ppr_stops_between_iterations():
    work = Activity()

    class Transition:
        shape = (3, 3)
        calls = 0

        def __matmul__(self, vector):
            self.calls += 1
            work.close_admission()
            return vector

    transition = Transition()
    with pytest.raises(WorkClosing):
        work.wrap("recommendation rebuild", ppr.ppr_rank, transition, [0], iters=500)()
    assert transition.calls == 1
    assert work.wait(0)


def test_cancelled_embedding_keeps_saved_vectors(store, monkeypatch):
    store.rec_model.replace_rec_vectors([("old", np.ones(8, dtype=np.float32).tobytes())])
    work = Activity()

    def finish_after_quit(*args, **kwargs):
        work.close_admission()
        return ["new"], np.ones((1, 8), dtype=np.float32)

    monkeypatch.setattr(embed, "build_vectors_from_baskets", finish_after_quit)
    with pytest.raises(WorkClosing):
        work.wrap("recommendation rebuild", model_refresh.rebuild, store, 8)()
    assert [key for key, _ in store.rec_model.get_rec_vectors()] == ["old"]


def test_quit_exits_process_with_sql_holding_database_and_default_executor_waiting(tmp_path):
    ready, output = tmp_path / "ready", tmp_path / "child.log"
    # A separate process exercises Python's executor shutdown too. A thread-only
    # server test misses the hang after Uvicorn logs "Finished server process".
    code = '''
import asyncio
import logging
import sys
import threading
from pathlib import Path
import uvicorn
from yt_playlist.__main__ import AppServer
from yt_playlist.core.store import Store
from yt_playlist.web.app import create_app

logging.basicConfig(level=logging.INFO)
store = Store(":memory:")
store.init_schema()
app = create_app(store, lambda: {}, background_work=False)
worker = app.state.ctx.rec_worker
worker.enabled = True
worker.debounce_s = 0
entered = threading.Event()
store.conn.create_function("query_started", 0, lambda: (entered.set(), 0)[1])
def query():
    with store._lock:
        store.conn.execute(sys.argv[2]).fetchone()
worker._rebuild = lambda: worker._stage("trend rollups", query)

class Server(AppServer):
    async def startup(self, sockets=None):
        await super().startup(sockets)
        worker.trigger()
        assert await asyncio.to_thread(entered.wait, 5)
        # Simulate a page request waiting for the database lock in the default
        # executor. asyncio cancellation alone cannot stop that thread.
        asyncio.create_task(asyncio.to_thread(store.settings.get_setting, "queued"))
        await asyncio.sleep(0.05)
        Path(sys.argv[1]).write_text("ready")

Server(uvicorn.Config(app, host="127.0.0.1", port=0,
    timeout_graceful_shutdown=0.1, log_config=None)).run()
'''
    with output.open("w") as logs:
        child = subprocess.Popen([sys.executable, "-c", code, str(ready), SLOW_SQL],
                                 stdout=logs, stderr=subprocess.STDOUT,
                                 cwd=Path(__file__).resolve().parents[1])
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            assert ready.exists(), output.read_text()
            child.send_signal(signal.SIGINT)
            child.wait(timeout=5)
            assert child.returncode in (0, -signal.SIGINT), output.read_text()
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
    logged = output.read_text()
    assert "rec worker stopped for shutdown" in logged
    assert "Application shutdown complete" in logged
    assert "did not stop" not in logged
    assert "Application shutdown failed" not in logged
