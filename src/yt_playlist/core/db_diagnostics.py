"""Time SQLite work separately from the serving lock, without logging SQL or values."""
from contextlib import closing, contextmanager
import hashlib
from itertools import islice
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
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
        while frame and frame.f_globals.get('__name__') in {__name__, HandleConnection.__module__}:
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


def _warn_storage(conn, error, stage):
    code = getattr(error, 'sqlite_errorcode', 0)
    if code & 255 not in (sqlite3.SQLITE_FULL, sqlite3.SQLITE_IOERR):
        return
    try:
        detail = _storage_detail(conn, error, stage)
    except Exception as diagnostic_error:
        # Diagnostics must preserve the original storage error even when
        # resolving a temporary directory or inspecting capacity also fails.
        detail = dict(pid=os.getpid(), code=code, stage=stage,
                      diagnostic_error=type(diagnostic_error).__name__)
    logger.warning('SQLite storage failure: %s', json.dumps(detail, sort_keys=True))


def _storage_detail(conn, error, stage):
    code = error.sqlite_errorcode
    detail = dict(pid=os.getpid(), code=code, name=getattr(error, 'sqlite_errorname', ''),
                  stage=stage, operation=_operation.get(), phase=current_phase(), site=_site())
    paths = {'system_temp': '/tmp', 'cwd': os.getcwd(), 'python_temp': tempfile.gettempdir()}
    paths.update({key: os.environ[key] for key in ('TMPDIR', 'SQLITE_TMPDIR') if key in os.environ})
    try:
        # Bypass the diagnostic cursors to avoid recursive error reporting.
        # The connection mutex also protects against another serving thread.
        with conn._sqlite_lock:
            with closing(sqlite3.Connection.execute(conn, 'PRAGMA database_list')) as cursor:
                detail['databases'] = [tuple(row) for row in cursor]
            detail['limits'] = {}
            for name in ('page_size', 'page_count', 'max_page_count', 'temp_store', 'journal_mode'):
                with closing(sqlite3.Connection.execute(conn, 'PRAGMA main.' + name)) as cursor:
                    detail['limits'][name] = cursor.fetchone()[0]
        for _, schema, path in detail['databases']:
            if path:
                paths['database_' + schema] = str(Path(path).parent)
    except Exception as diagnostic_error:
        detail['sqlite_probe_error'] = type(diagnostic_error).__name__
    detail['filesystems'] = {}
    for name, path in paths.items():
        try:
            usage = os.statvfs(path)
            detail['filesystems'][name] = dict(path=path, device=os.stat(path).st_dev,
                available_bytes=usage.f_bavail * usage.f_frsize, available_inodes=usage.f_favail)
        except (OSError, AttributeError) as diagnostic_error:
            detail['filesystems'][name] = dict(path=path, error=type(diagnostic_error).__name__)
    # SQLite immediately unlinks its temporary files. A directory-size scan
    # misses them; inspect a bounded number of this process's own descriptors.
    detail['sqlite_temporary_files'] = []
    try:
        for descriptor in islice(Path('/proc/self/fd').iterdir(), 256):
            try:
                target = str(descriptor.readlink())
                if 'etilqs_' in target:
                    info = descriptor.stat()
                    detail['sqlite_temporary_files'].append(dict(
                        path=target, bytes=info.st_size, allocated_bytes=info.st_blocks * 512))
            except OSError:
                continue
    except OSError:
        pass
    return detail


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
            _warn_storage(self.connection, exc, stage)
            if (getattr(exc, 'sqlite_errorcode', 0) & 255 == sqlite3.SQLITE_READONLY
                    and getattr(self.connection, '_storage_compacting', False)):
                from yt_playlist.core.db_space import StorageCompacting
                self.connection.rollback()
                raise StorageCompacting() from exc
            raise
        finally:
            # Sum only time inside SQLite, excluding Python work between fetches.
            try:
                self._elapsed += monotonic() - started
                if self._elapsed >= SLOW_SQL_SECONDS and (not self._reported or error is not None):
                    self._reported = True
                    _warn_sql(self._sql, self._elapsed, stage, error)
            finally:
                # Do not create an exception/frame/cursor cycle of our own.
                error = None

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
        return super().execute(sql, parameters)

    def executemany(self, sql, parameters):
        return super().executemany(sql, parameters)

    def executescript(self, sql):
        if getattr(self, '_atomic_unit', False):
            raise RuntimeError('Scripts cannot implicitly commit an atomic unit')
        return super().executescript(sql)

    def _finish(self, sql, target, *args):
        started = monotonic()
        error = None
        try:
            return target(*args)
        except BaseException as exc:
            error = exc
            _warn_storage(self, exc, 'transaction')
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
