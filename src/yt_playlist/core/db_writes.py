"""Bounded writer admission without a SQLite busy wait under ServingLock."""
from contextlib import contextmanager, ExitStack, nullcontext
import sqlite3
import time

from .activity import check_cancellable_work


class _AdmissionBusy(Exception):
    pass


@contextmanager
def atomic_write(store, *, wait_seconds=1):
    """Reserve before reading mutable input; retry contention outside the lock.

    Repository commits defer to this unit. A caller's existing transaction keeps
    ownership through a savepoint. A caller already holding ServingLock cannot
    release that outer lock here, so contention is rejected immediately.
    """
    with _admitted(store, _atomic_unit, wait_seconds=wait_seconds):
        yield


@contextmanager
def _atomic_unit(conn):
    with conn.atomic(join=True):
        # A joined deferred transaction also needs admission before its first
        # read of the mutable owner.
        conn.execute('UPDATE main.settings SET value=value WHERE 0')
        yield


@contextmanager
def reserved_write(store, *, timings=None, wait_seconds=1):
    """Own an idle connection for a publisher that explicitly commits/rolls back."""
    with ExitStack() as unit:
        with timings.measure('writer_admission') if timings else nullcontext():
            unit.enter_context(_admitted(store, _reserved_unit, wait_seconds=wait_seconds, timings=timings))
        yield


@contextmanager
def _reserved_unit(conn):
    if conn.in_transaction:
        raise RuntimeError('Cannot publish maintenance inside another transaction')
    try:
        conn.execute('BEGIN IMMEDIATE')
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


@contextmanager
def _admitted(store, enter, *, wait_seconds, timings=None):
    inherited_lock = store._lock.held_by_current_thread
    deadline = time.monotonic() + wait_seconds
    while True:
        check_cancellable_work()
        with timings.locked(store._lock) if timings else store._lock:
            conn = store.conn
            previous_timeout = conn.execute('PRAGMA busy_timeout').fetchone()[0]
            conn.execute('PRAGMA busy_timeout=0')
            try:
                try:
                    with ExitStack() as unit:
                        try:
                            with timings.measure('reserve_writer') if timings else nullcontext():
                                unit.enter_context(enter(conn))
                        except sqlite3.OperationalError as error:
                            if getattr(error, 'sqlite_errorcode', 0) & 255 not in (
                                    sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                                raise
                            raise _AdmissionBusy(error) from error
                        yield
                        return
                except _AdmissionBusy as blocked:
                    error = blocked.args[0]
            finally:
                conn.execute(f'PRAGMA busy_timeout={previous_timeout}')
        remaining = deadline - time.monotonic()
        if inherited_lock or remaining <= 0:
            raise error
        check_cancellable_work()
        time.sleep(min(.01, remaining))
