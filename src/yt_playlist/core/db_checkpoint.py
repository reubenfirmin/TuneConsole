"""Keep maintenance WAL checkpoints outside the serving connection lock.

The commit keeps SQLite's configured durability. Only the automatic copy of committed WAL
pages back into the database is deferred; a private connection checkpoints them passively.
"""
from contextlib import closing, contextmanager
import logging
import sqlite3

from yt_playlist.core.db_diagnostics import DiagnosticConnection

log = logging.getLogger(__name__)


@contextmanager
def deferred_autocheckpoint(conn, enabled):
    """Yield whether to checkpoint later; restore policy before releasing the serving lock."""
    if not enabled:
        yield False
        return
    threshold = conn.execute('PRAGMA wal_autocheckpoint').fetchone()[0]
    if not threshold:
        # An explicitly disabled automatic policy also opts out of our replacement checkpoint.
        yield False
        return
    conn.execute('PRAGMA wal_autocheckpoint=0')
    try:
        yield True
    finally:
        restore = f'PRAGMA wal_autocheckpoint={int(threshold)}'
        try:
            conn.execute(restore)
        except sqlite3.OperationalError as exc:
            if getattr(exc, 'sqlite_errorcode', None) != sqlite3.SQLITE_INTERRUPT:
                raise
            # The activity progress handler interrupts once per cancellation scope. Restore
            # connection policy even if cancellation first arrives just after durable commit.
            conn.execute(restore)


def checkpoint_wal(path):
    """Best effort after publication. A failure cannot undo or discredit a durable commit.

    PASSIVE does not wait for readers/writers or truncate the WAL. It may leave pages behind
    for the next checkpoint; the serving connection retains its original automatic policy.
    """
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=0,
                                     factory=DiagnosticConnection)) as conn:
            return conn.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()
    except sqlite3.Error:
        log.warning('Post-publication WAL checkpoint deferred', exc_info=True)
        return None
