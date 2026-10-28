"""Short-lived committed-data reads without publication revision tracking."""
from contextlib import contextmanager

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
            reader.conn.execute('PRAGMA query_only=ON')
            reader.conn.execute('BEGIN')
            reader.conn.execute('SELECT 1 FROM sqlite_schema LIMIT 1').fetchone()
            yield reader
        finally:
            try:
                if reader is not None:
                    reader.conn.close()
            finally:
                source._analytics_readers.release()
