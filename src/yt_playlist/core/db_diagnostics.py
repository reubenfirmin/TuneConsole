"""Time SQLite work separately from the serving lock, without logging SQL or values."""
from contextlib import contextmanager
import hashlib
import logging
from pathlib import Path
import sqlite3
import sys
from time import monotonic, thread_time

from yt_playlist.core.work_diagnostics import current_phase
from yt_playlist.core.db_handles import Connection as HandleConnection, Cursor as HandleCursor
from yt_playlist.core.lock_diagnostics import (
    LockWaitDiagnostics, SLOW_LOCK_SECONDS, _operation, _outcome,
    cooperative_lock_yield, database_lock_wait, database_operation,
)

logger = logging.getLogger("yt_playlist.database")
SLOW_SQL_SECONDS = .25


def _site():
    frame = sys._getframe(1)
    try:
        while frame and frame.f_code.co_filename == __file__:
            frame = frame.f_back
        return f"{Path(frame.f_code.co_filename).name}:{frame.f_lineno}" if frame else "unknown"
    finally:
        del frame


def _warn_sql(sql, elapsed, stage, error):
    verb = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
    if verb not in {"SELECT", "WITH", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP",
                    "PRAGMA", "COMMIT", "ROLLBACK", "BEGIN", "SAVEPOINT", "RELEASE", "VACUUM",
                    "REINDEX", "ANALYZE"}:
        verb = "statement"
    logger.warning("Slow SQL: operation=%s phase=%s elapsed=%.3fs stage=%s outcome=%s "
                   "statement=%s sql_id=%s site=%s", _operation.get(), current_phase(), elapsed,
                   stage, _outcome(error), verb, hashlib.sha256(sql.encode()).hexdigest()[:12], _site())


class PublicationTimings:
    """Separate maintenance lock ownership, writes and durability work without logging data."""

    def __init__(self):
        self.stages = {}

    @contextmanager
    def measure(self, name):
        started = monotonic(), thread_time()
        try:
            yield
        finally:
            self._record(name, started)

    def _record(self, name, started):
        self.stages[name] = {'wall_s': monotonic() - started[0],
                             'cpu_s': thread_time() - started[1]}

    @contextmanager
    def locked(self, lock):
        started = monotonic(), thread_time()
        with lock:
            self._record('lock_wait', started)
            with self.measure('lock'):
                yield

    def report(self, outcome):
        held = self.stages.get('lock', {}).get('wall_s', 0)
        logger.log(logging.INFO if held >= SLOW_LOCK_SECONDS else logging.DEBUG,
                   'Maintenance publication: phase=%s outcome=%s stages=%s', current_phase(),
                   outcome, {name: {key: round(value, 6) for key, value in times.items()}
                             for name, times in self.stages.items()})


class DiagnosticCursor(HandleCursor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._start("")

    def _start(self, sql):
        self._sql, self._elapsed, self._reported = sql, 0., False

    def _call(self, stage, target, *args):
        started = monotonic()
        error = None
        try:
            return target(*args)
        except StopIteration:
            raise
        except BaseException as exc:
            error = exc
            if (getattr(exc, 'sqlite_errorcode', 0) & 255 == sqlite3.SQLITE_READONLY
                    and getattr(self.connection, '_storage_compacting', False)):
                from yt_playlist.core.db_space import StorageCompacting
                self.connection.rollback()
                raise StorageCompacting() from exc
            raise
        finally:
            # Sum only time inside SQLite, excluding Python work between fetches.
            self._elapsed += monotonic() - started
            if self._elapsed >= SLOW_SQL_SECONDS and (not self._reported or error is not None):
                self._reported = True
                _warn_sql(self._sql, self._elapsed, stage, error)

    def execute(self, sql, parameters=()):
        self._start(sql)
        return self._call("execute", super().execute, sql, parameters)

    def executemany(self, sql, parameters):
        self._start(sql)
        return self._call("executemany", super().executemany, sql, parameters)

    def executescript(self, sql):
        self._start(sql)
        return self._call("executescript", super().executescript, sql)

    def fetchone(self):
        return self._call("fetchone", super().fetchone)

    def fetchmany(self, size=None):
        args = () if size is None else (size,)
        return self._call("fetchmany", super().fetchmany, *args)

    def fetchall(self):
        return self._call("fetchall", super().fetchall)

    def __next__(self):
        return self._call("iterate", super().__next__)


class DiagnosticConnection(HandleConnection):
    @contextmanager
    def atomic(self, *, join=False):
        """Commit an admitted unit and its receipt together across repository calls.

        Repository helpers normally finish their own writes. During this explicit
        outer unit, their commits defer to its owner; any rollback aborts the unit.
        The caller must hold its connection lock throughout.

        With join=True an existing caller transaction retains commit/rollback
        ownership. A savepoint rolls back only this unit if it fails.
        """
        if getattr(self, '_atomic_unit', False):
            if not join:
                raise RuntimeError('An atomic unit requires an idle connection')
            try:
                yield
            except BaseException:
                self._atomic_failed = True
                raise
            return
        borrowed = self.in_transaction
        if borrowed and not join:
            raise RuntimeError('An atomic unit requires an idle connection')
        self.execute('SAVEPOINT tuneconsole_atomic_unit' if borrowed else 'BEGIN IMMEDIATE')
        self._atomic_unit = True
        self._atomic_joined = borrowed
        self._atomic_failed = False
        try:
            yield
            if self._atomic_failed:
                raise RuntimeError('A repository rolled back the atomic unit')
            if borrowed:
                self.execute('RELEASE SAVEPOINT tuneconsole_atomic_unit')
            else:
                self._finish('COMMIT', super().commit)
        except BaseException:
            if borrowed:
                # SQLite can abort the whole transaction on an interrupted
                # write, removing its savepoints along with the pending rows.
                if self.in_transaction:
                    self.execute('ROLLBACK TO SAVEPOINT tuneconsole_atomic_unit')
                    self.execute('RELEASE SAVEPOINT tuneconsole_atomic_unit')
            else:
                self._finish('ROLLBACK', super().rollback)
            raise
        finally:
            self._atomic_unit = False
            self._atomic_joined = False

    def cursor(self, factory=DiagnosticCursor):
        return super().cursor(factory)

    def execute(self, sql, parameters=()):
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql, parameters):
        return self.cursor().executemany(sql, parameters)

    def executescript(self, sql):
        if getattr(self, '_atomic_unit', False):
            raise RuntimeError('Scripts cannot implicitly commit an atomic unit')
        return self.cursor().executescript(sql)

    def _finish(self, sql, target, *args):
        started = monotonic()
        error = None
        try:
            return target(*args)
        except BaseException as exc:
            error = exc
            raise
        finally:
            elapsed = monotonic() - started
            if elapsed >= SLOW_SQL_SECONDS:
                _warn_sql(sql, elapsed, "transaction", error)

    def commit(self):
        if getattr(self, '_atomic_unit', False):
            return
        return self._finish("COMMIT", super().commit)

    def rollback(self):
        if getattr(self, '_atomic_unit', False):
            self._atomic_failed = True
            if self._atomic_joined:
                return
        return self._finish("ROLLBACK", super().rollback)

    def __exit__(self, *args):
        if getattr(self, '_atomic_unit', False):
            self._atomic_failed |= args[0] is not None
            return False
        return self._finish("COMMIT" if args[0] is None else "ROLLBACK", super().__exit__, *args)
