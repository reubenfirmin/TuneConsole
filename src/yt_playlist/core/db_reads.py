"""Short-lived committed-data reads without publication revision tracking."""
from contextlib import contextmanager, ExitStack, nullcontext
from collections import OrderedDict
from copy import deepcopy
import logging
import threading
import time

from yt_playlist.core.activity import check_cancellable_work, work_budget
from yt_playlist.core.lock_diagnostics import _operation
from yt_playlist.core.work_diagnostics import current_phase

MAX_READERS = 4
READ_SECONDS = 10
logger = logging.getLogger('yt_playlist.database')


class ReaderDiagnostics:
    """Retain only active read owners; report contention with bounded frequency."""

    def __init__(self):
        self.lock = threading.Lock()
        self.owners = {}
        self.next_report = 0.

    @contextmanager
    def owner(self, background):
        token = object()
        with self.lock:
            self.owners[token] = dict(tid=threading.get_native_id(), operation=_operation.get(),
                phase=current_phase(), background=background, started=time.monotonic())
        try:
            yield
        finally:
            with self.lock:
                self.owners.pop(token, None)

    def waiting(self, started, background):
        now = time.monotonic()
        if now - started < .25 or now < self.next_report:
            return
        with self.lock:
            if now < self.next_report:
                return
            self.next_report = now + 1
            owners = [{**{key: value for key, value in owner.items() if key != 'started'},
                       'held_s': round(now - owner['started'], 3)} for owner in self.owners.values()]
        logger.warning('Reader admission wait: operation=%s phase=%s background=%s elapsed=%.3fs owners=%s',
                       _operation.get(), current_phase(), background, now - started, owners)


@contextmanager
def _admission(source):
    # Cooperative work yields while retaining its read snapshot. If it consumes
    # every slot, the foreground request it yields to cannot open a reader.
    # Reserve one of the existing four slots, without increasing the total.
    background = source.priority.is_background
    gates = ([source._background_readers] if background else []) + [source._analytics_readers]
    acquired = []
    started = time.monotonic()
    try:
        for gate in gates:
            while not gate.acquire(timeout=.025):
                source._reader_diagnostics.waiting(started, background)
                check_cancellable_work()
            acquired.append(gate)
            check_cancellable_work()
        with source._reader_diagnostics.owner(background):
            yield
    finally:
        for gate in reversed(acquired):
            gate.release()


@contextmanager
def read_view(source, make_reader, *, tracked=False, seconds=...):
    """Bound admission and query time; never retain a connection after the call."""
    # Explicit worker computations may retain a snapshot while doing substantial
    # CPU work. Their cancellation owner, rather than an HTTP read deadline,
    # governs that lifetime. Ordinary request readers retain the default bound.
    seconds = READ_SECONDS if seconds is ... else seconds
    with (work_budget(seconds) if seconds is not None else nullcontext()), _admission(source):
        reader = None
        try:
            check_cancellable_work()
            if tracked:
                # Foreground composition keeps the same admission/deadline as
                # ordinary views while reusing reads guarded by real revisions.
                from yt_playlist.core.db_snapshot import read_snapshot, SnapshotBusy
                # Long-running preparation must not evict interactive results.
                # Reuse is optional: a busy writer must not delay committed
                # file-backed reads just to capture or validate cache guards.
                with ExitStack() as tracked_view:
                    try:
                        snapshot = tracked_view.enter_context(read_snapshot(
                            source, make_reader, memo=source._foreground_read_memo,
                            wait_for_writer=source._db_path is None))
                    except SnapshotBusy:
                        pass
                    else:
                        yield snapshot.store
                        return
            reader = make_reader()
            if source._db_path is None:
                # In-memory fixtures have no shared database file. Production
                # file readers pin WAL state without copying database pages.
                with source._lock:
                    if source.conn.in_transaction:
                        raise RuntimeError('Cannot copy uncommitted changes for analytics')
                    source.conn.backup(reader.conn, pages=128,
                        progress=lambda *_: check_cancellable_work())
                    reader._bind_repositories()
            reader.conn.execute('PRAGMA query_only=ON')
            if not reader.conn.in_transaction:
                reader.conn.execute('BEGIN')
            reader.conn.execute('SELECT 1 FROM sqlite_schema LIMIT 1').fetchone()
            # A composed Home response revisits the same readiness checks.
            # This pinned, read-only view cannot change underneath them. Keep
            # detached results only for its lifetime, with the same small entry
            # bound as background snapshot memoization.
            memo = OrderedDict()
            def calculate(name, prepare):
                if name in memo:
                    memo.move_to_end(name)
                    return deepcopy(memo[name])
                value = prepare()
                memo[name] = deepcopy(value)
                if len(memo) > 32:
                    memo.popitem(last=False)
                return value
            reader._memoized_read = calculate
            yield reader
        finally:
            if reader is not None:
                reader._memoized_read = None
                reader.conn.close()
