"""Short-lived committed-data reads without publication revision tracking."""
from contextlib import contextmanager
from collections import OrderedDict
from copy import deepcopy

from yt_playlist.core.activity import check_cancellable_work, work_budget

MAX_READERS = 4
READ_SECONDS = 10


@contextmanager
def read_view(source, make_reader):
    """Bound admission and query time; never retain a connection after the call."""
    with work_budget(READ_SECONDS):
        while not source._analytics_readers.acquire(timeout=0.025):
            check_cancellable_work()
        reader = None
        try:
            check_cancellable_work()
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
            try:
                if reader is not None:
                    reader._memoized_read = None
                    reader.conn.close()
            finally:
                source._analytics_readers.release()
