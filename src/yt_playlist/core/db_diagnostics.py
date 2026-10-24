"""Time SQLite work separately from the serving lock, without logging SQL or values."""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import logging
from pathlib import Path
import sqlite3
import sys
import threading
from time import monotonic, thread_time

from yt_playlist.core.work_diagnostics import current_phase

logger = logging.getLogger("yt_playlist.database")
SLOW_SQL_SECONDS = .25
SLOW_LOCK_SECONDS = .1
_operation = ContextVar("tuneconsole_database_operation", default="direct")
_lock_timing = ContextVar("tuneconsole_lock_timing", default=None)


@contextmanager
def cooperative_lock_yield():
    """Attribute only scheduler condition waits to the current lock acquisition."""
    timing = _lock_timing.get()
    if timing is None:
        yield
        return
    started = monotonic()
    try:
        yield
    finally:
        timing[0] += monotonic() - started


class LockWaitDiagnostics:
    """Per-serving-lock, bounded background summaries; foreground and failures stay immediate."""

    def __init__(self, *, interval=60., max_groups=32):
        self.interval, self.max_groups = interval, max_groups
        self._lock = threading.Lock()
        self.started = None
        self.groups = {}

    def record(self, wait, yielded, outcome, *, background):
        now = monotonic()
        interesting = wait + yielded >= SLOW_LOCK_SECONDS or outcome in {'failed', 'cancelled'}
        started = self.started
        if not interesting and (started is None or now - started < self.interval):
            return
        with self._lock:
            self._flush(now)
            if not interesting:
                return
            operation, phase = _operation.get(), current_phase()
            logger.debug('Database lock acquisition: operation=%s phase=%s background=%s '
                         'wait=%.3fs yield=%.3fs outcome=%s', operation, phase, background,
                         wait, yielded, outcome)
            warn = wait >= SLOW_LOCK_SECONDS or outcome in {'failed', 'cancelled'}
            if background and outcome == 'ok':
                if self.started is None:
                    self.started = now
                key = operation, phase
                if key not in self.groups and len(self.groups) >= self.max_groups:
                    key = 'other', 'other'
                stats = self.groups.setdefault(key, dict(count=0, wait_total=0., wait_max=0.,
                    yield_total=0., yield_max=0., warned_wait=0.))
                stats['count'] += 1
                stats['wait_total'] += wait
                stats['wait_max'] = max(stats['wait_max'], wait)
                stats['yield_total'] += yielded
                stats['yield_max'] = max(stats['yield_max'], yielded)
                # First real contention and each doubling remain actionable within the interval.
                warn = warn and wait >= max(SLOW_LOCK_SECONDS, 2 * stats['warned_wait'])
                if warn:
                    stats['warned_wait'] = wait
            if warn:
                logger.warning('Slow database lock wait: operation=%s phase=%s elapsed=%.3fs '
                               'yield=%.3fs outcome=%s background=%s', operation, phase,
                               wait, yielded, outcome, background)

    def flush(self, *, force=False):
        with self._lock:
            self._flush(monotonic(), force=force)

    def _flush(self, now, *, force=False):
        if self.started is None or (not force and now - self.started < self.interval):
            return
        for (operation, phase), stats in self.groups.items():
            logger.info('Database lock summary: interval=%.1fs operation=%s phase=%s outcome=ok '
                        'count=%d wait_total=%.3fs wait_max=%.3fs yield_total=%.3fs yield_max=%.3fs',
                        now - self.started, operation, phase, stats['count'], stats['wait_total'],
                        stats['wait_max'], stats['yield_total'], stats['yield_max'])
        self.groups.clear()
        self.started = None


@contextmanager
def database_operation(name):
    token = _operation.set(name)
    try:
        yield
    finally:
        _operation.reset(token)


def _outcome(error):
    if error is None:
        return "ok"
    if (getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT
            or type(error).__name__ in {"WorkClosing", "CancelledError", "KeyboardInterrupt"}):
        return "cancelled"
    return "failed"


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


def database_lock_wait(acquire, *args, background=False, diagnostics=None):
    started = monotonic()
    timing = [0.]
    token = _lock_timing.set(timing)
    error = None
    acquired = False
    try:
        acquired = acquire(*args)
        return acquired
    except BaseException as exc:
        error = exc
        raise
    finally:
        elapsed = monotonic() - started
        _lock_timing.reset(token)
        yielded = min(elapsed, timing[0])
        outcome = _outcome(error) if error is not None or acquired else "busy"
        if diagnostics is None:
            diagnostics = LockWaitDiagnostics()
        diagnostics.record(elapsed - yielded, yielded, outcome, background=background)


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


class DiagnosticCursor(sqlite3.Cursor):
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


class DiagnosticConnection(sqlite3.Connection):
    def cursor(self, factory=DiagnosticCursor):
        return super().cursor(factory)

    def execute(self, sql, parameters=()):
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql, parameters):
        return self.cursor().executemany(sql, parameters)

    def executescript(self, sql):
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
        return self._finish("COMMIT", super().commit)

    def rollback(self):
        return self._finish("ROLLBACK", super().rollback)

    def __exit__(self, *args):
        return self._finish("COMMIT" if args[0] is None else "ROLLBACK", super().__exit__, *args)
