"""Failed SQL must not defer SQLite resource cleanup to the UI thread's GC."""
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from yt_playlist.core.db_diagnostics import DiagnosticConnection
from yt_playlist.core.db_handles import Connection


@pytest.mark.parametrize("factory", [Connection, DiagnosticConnection])
@pytest.mark.parametrize("method", ["execute", "executemany", "executescript"])
def test_failed_implicit_execution_closes_its_cursor(factory, method):
    connection = sqlite3.connect(":memory:", factory=factory)
    try:
        connection.execute("CREATE TABLE values_to_write(n)")
        if method == "execute":
            arguments = ("SELECT ?", (object(),))
        elif method == "executemany":
            arguments = ("INSERT INTO values_to_write VALUES (?)", [(object(),)])
        else:
            arguments = ("SELECT 1; SELECT missing_column;",)
        with pytest.raises(sqlite3.Error) as failure:
            getattr(connection, method)(*arguments)
        # Keep the actual traceback alive, as logging/request frameworks can.
        # No cursor it retains may still own a statement needing sqlite3_reset.
        cursors = tuple(connection._handle_cursors)
        assert cursors
        for cursor in cursors:
            with pytest.raises(sqlite3.ProgrammingError, match="closed cursor"):
                cursor.execute("SELECT 2")
        assert failure.value.__traceback__ is not None
        assert connection.execute("SELECT 3").fetchone() == (3,)
    finally:
        connection.close()


@pytest.mark.parametrize("factory", [Connection, DiagnosticConnection])
@pytest.mark.parametrize("fetch", ["fetchone", "fetchmany", "fetchall", "__next__"])
def test_fetch_failure_retires_implicit_cursor_but_explicit_cursor_is_reusable(factory, fetch):
    connection = sqlite3.connect(":memory:", factory=factory)
    try:
        def invalid_row(cursor, row):
            raise ValueError("controlled row conversion failure")

        connection.row_factory = invalid_row
        implicit = connection.execute("SELECT 1 UNION ALL SELECT 2")
        explicit = connection.cursor().execute("SELECT 1 UNION ALL SELECT 2")
        for cursor in (implicit, explicit):
            with pytest.raises(ValueError, match="controlled row conversion failure"):
                getattr(cursor, fetch)()
        with pytest.raises(sqlite3.ProgrammingError, match="closed cursor"):
            implicit.execute("SELECT 3")
        explicit.row_factory = None
        assert explicit.execute("SELECT 4").fetchone() == (4,)
        explicit.close()
    finally:
        connection.close()


@pytest.mark.parametrize("factory", [Connection, DiagnosticConnection])
def test_cursor_retirement_preserves_the_callers_transaction(factory):
    connection = sqlite3.connect(":memory:", factory=factory)
    try:
        connection.execute('CREATE TABLE mutations(n INTEGER UNIQUE)')
        connection.execute('INSERT INTO mutations VALUES (1)')
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute('INSERT INTO mutations VALUES (1)')
        assert connection.in_transaction
        connection.execute('INSERT INTO mutations VALUES (2)')
        connection.commit()
        assert connection.execute('SELECT n FROM mutations ORDER BY n').fetchall() == [(1,), (2,)]
        connection.execute('INSERT INTO mutations VALUES (3)')
        connection.rollback()
        assert connection.execute('SELECT n FROM mutations ORDER BY n').fetchall() == [(1,), (2,)]
    finally:
        connection.close()


@pytest.mark.parametrize("quit_signal", [signal.SIGINT, signal.SIGTERM])
def test_collecting_failed_query_tracebacks_cannot_block_server_quit(tmp_path, quit_signal):
    collecting, collected = tmp_path / "collecting", tmp_path / "collected"
    output = tmp_path / "server.log"
    code = '''
import asyncio
import faulthandler
import gc
import logging
import sqlite3
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import uvicorn
from fastapi import FastAPI
from yt_playlist.__main__ import AppServer
from yt_playlist.core.activity import Activity
from yt_playlist.core.db_diagnostics import DiagnosticConnection

logging.basicConfig(level=logging.INFO)
faulthandler.dump_traceback_later(5)
gc.disable()
connection = sqlite3.connect(":memory:", factory=DiagnosticConnection, check_same_thread=False)
def invalid_row(cursor, row):
    raise ValueError("controlled row conversion failure")
connection.row_factory = invalid_row
def failed_read():
    try:
        connection.execute("SELECT 1 UNION ALL SELECT 2").fetchone()
    except ValueError:
        pass
failed_read()
connection.row_factory = None

entered, release = threading.Event(), threading.Event()
def hold_connection():
    entered.set()
    release.wait()
    return 1
connection.create_function("hold_connection", 0, hold_connection)
def worker():
    connection.execute("SELECT hold_connection()").fetchall()
thread = threading.Thread(target=worker, daemon=True)
def collect():
    Path(sys.argv[1]).write_text("collecting")
    gc.collect()
    Path(sys.argv[2]).write_text("collected")

@asynccontextmanager
async def lifespan(app):
    thread.start()
    assert await asyncio.to_thread(entered.wait, 5)
    asyncio.get_running_loop().call_soon(collect)
    try:
        yield
    finally:
        release.set()
        await asyncio.to_thread(thread.join, 2)
        assert not thread.is_alive()
        connection.close()
        faulthandler.cancel_dump_traceback_later()

app = FastAPI(lifespan=lifespan)
app.state.ctx = SimpleNamespace(work=Activity())
AppServer(uvicorn.Config(app, host="127.0.0.1", port=0,
    timeout_graceful_shutdown=.1, log_config=None)).run()
'''
    with output.open("w") as logs:
        child = subprocess.Popen([sys.executable, "-c", code, str(collecting), str(collected)],
                                 stdout=logs, stderr=subprocess.STDOUT,
                                 cwd=Path(__file__).resolve().parents[2])
        try:
            deadline = time.monotonic() + 30
            while not collecting.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            assert collecting.exists(), output.read_text()
            child.send_signal(quit_signal)
            try:
                child.wait(timeout=6)
            except subprocess.TimeoutExpired:
                pytest.fail("Cursor garbage collection blocked server shutdown:\n" + output.read_text())
            assert child.returncode in (0, -quit_signal), output.read_text()
            assert collected.exists(), output.read_text()
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
    assert "Application shutdown complete" in output.read_text()
