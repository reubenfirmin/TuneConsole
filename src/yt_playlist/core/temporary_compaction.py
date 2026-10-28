"""Compact in the OS temporary directory, then restore in one SQLite transaction.

Never replace an open database file or move its WAL. A reserved writer protects
VACUUM INTO; the backup API protects installation. Check data_version after its
first incomplete step, while it owns the writer, to close the handoff race.
"""
from contextlib import closing
import logging
from pathlib import Path
import sqlite3
import tempfile
import time

from yt_playlist.core import db_space
from yt_playlist.core.policy_recovery import _verify

log = logging.getLogger(__name__)
COPY_ALLOWANCE = 4 * 1024 * 1024
WRITER_WAIT_SECONDS = 1.0


def _error(message, code):
    error = sqlite3.OperationalError(message)
    error.sqlite_errorcode = code
    return error


def _check_cancelled(cancelled):
    if cancelled():
        raise _error('Storage compaction interrupted', sqlite3.SQLITE_INTERRUPT)


def _open(path, *, readonly=False):
    mode = 'ro' if readonly else 'rw'
    return sqlite3.connect(path.as_uri() + f'?mode={mode}&cache=private', uri=True, timeout=.1)


def _counts(conn):
    tables = conn.execute("SELECT name FROM sqlite_schema WHERE type='table' "
                          "AND name NOT LIKE 'sqlite_%'").fetchall()
    return {name: conn.execute('SELECT COUNT(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
            for (name,) in tables}


def _restore(candidate, path, observed, version, cancelled):
    pages = candidate.execute('PRAGMA page_count').fetchone()[0]
    if pages < 2:
        raise ValueError('Compaction candidate has no application schema')
    checked = False
    deadline = time.monotonic() + WRITER_WAIT_SECONDS

    def progress(status, remaining, total):
        nonlocal checked
        if status != sqlite3.SQLITE_DONE:
            _check_cancelled(cancelled)
        if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            if time.monotonic() >= deadline:
                raise _error('Storage compaction writer is busy; retry with current data', sqlite3.SQLITE_BUSY)
            return
        if status not in (sqlite3.SQLITE_OK, sqlite3.SQLITE_DONE):
            # Let sqlite3_backup_finish preserve the actual FULL/IOERR/etc.
            # failure, including an error during the very first copy step.
            return
        if not checked:
            # pages < total guarantees this callback precedes COMMIT. Raising
            # here makes backup_finish roll back every destination page copied.
            if status != sqlite3.SQLITE_OK or remaining <= 0:
                raise RuntimeError('Compaction backup did not reserve a verifiable write transaction')
            if observed.execute('PRAGMA data_version').fetchone()[0] != version:
                raise _error('Database changed during compaction handoff; retry with current data', sqlite3.SQLITE_BUSY)
            checked = True

    with closing(_open(path)) as destination:
        if destination.execute('PRAGMA journal_mode').fetchone()[0].lower() != 'wal':
            raise ValueError('Transactional compaction requires WAL')
        destination.execute('PRAGMA synchronous=FULL')
        # Commit the new image first; the existing resumable checkpoint stage
        # reclaims its space after readers of the old image have drained.
        destination.execute('PRAGMA wal_autocheckpoint=0')
        candidate.backup(destination, pages=min(256, pages - 1), progress=progress, sleep=.05)


def compact(conn, directory, *, cancelled=lambda: False):
    """Leave the original intact on build/verification/installation failure."""
    if conn.in_transaction:
        raise RuntimeError('Storage compaction requires an idle source connection')
    if conn.execute('PRAGMA journal_mode').fetchone()[0].lower() != 'wal':
        raise ValueError('Transactional compaction requires WAL')
    path = Path(conn.execute('PRAGMA database_list').fetchone()[2]).resolve()
    page_size = conn.execute('PRAGMA page_size').fetchone()[0]
    used = (conn.execute('PRAGMA page_count').fetchone()[0]
            - conn.execute('PRAGMA freelist_count').fetchone()[0]) * page_size
    _check_cancelled(cancelled)
    with tempfile.TemporaryDirectory(prefix='tuneconsole-compact-') as temporary:
        scratch = Path(temporary)
        output = scratch / 'compact.db'
        db_space.require_space(scratch, used + COPY_ALLOWANCE)
        with closing(_open(path)) as guard:
            guard.execute('BEGIN IMMEDIATE')
            version = conn.execute('PRAGMA data_version').fetchone()[0]
            expected = _counts(conn)
            log.info('Building compact database in OS temporary directory %s', scratch)
            conn.execute('VACUUM INTO ?', (str(output),))
            output.chmod(0o600)
            with closing(_open(output, readonly=True)) as candidate:
                candidate.set_progress_handler(lambda: int(cancelled()), 1000)
                _verify(candidate)
                if _counts(candidate) != expected:
                    raise ValueError('Storage compaction changed application table counts')
                if candidate.execute('PRAGMA page_size').fetchone()[0] != page_size:
                    raise ValueError('Storage compaction changed the database page size')
                # One frame per compact page, including WAL frame headers. The
                # temporary output is already allocated, so this also handles
                # an OS temp directory on the database's own filesystem.
                pages = candidate.execute('PRAGMA page_count').fetchone()[0]
                db_space.require_space(directory, pages * (page_size + 24) + 32 + COPY_ALLOWANCE)
                _check_cancelled(cancelled)
                guard.rollback()
                _restore(candidate, path, conn, version, cancelled)
                log.info('Installed verified temporary compact database: bytes=%s', output.stat().st_size)
