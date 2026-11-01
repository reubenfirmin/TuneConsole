"""Coherent, read-only source snapshots of playback captures and main receipts.

The inbox fence protects captures and acknowledgements only until its small
snapshot and files are copied and the main SQLite view is pinned. Release it
before copying the large library so playback capture can continue during backup.
"""
from contextlib import closing, contextmanager
import hashlib
from pathlib import Path
import sqlite3
import time

from yt_playlist.core import policy_documents
from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.playback_inbox import Inbox


def _queue(database):
    return Inbox(Path(database).resolve(), None)


def _reader(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    return conn


def _pin(conn):
    conn.execute('BEGIN')
    conn.execute('SELECT rootpage FROM sqlite_schema LIMIT 1').fetchone()


def _copy(source, target):
    """Validate every capture before publishing its independently owned copy."""
    target.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with target.path.open('xb'):
        pass
    with target.admission(), closing(target.connect()) as copied:
        with copied:
            for row in source.execute('SELECT * FROM playback_feedback_inbox ORDER BY sequence'):
                Inbox.body(row)
                encoded = Path(row['filename']).read_bytes()
                if (len(encoded) != row['byte_size']
                        or hashlib.sha256(encoded).hexdigest() != row['content_digest']):
                    raise ValueError('Playback observation changed while backing up')
                filename = policy_documents._publish_encoded(target.directory, encoded, row['content_digest'])
                copied.execute('INSERT INTO playback_feedback_inbox VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (row['sequence'], row['capture_id'], row['kind'], row['account_id'], row['observed_at'],
                     row['video_id'], row['rating_status'], str(filename), row['content_digest'], row['byte_size']))
    with target.path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    return {'format': 1, 'database': str(target.path), 'sha256': digest}


@contextmanager
def pinned(conn, target, *, backup_root=None):
    """Yield the feedback manifest with the main view pinned and capture unblocked.

    The source inbox and its existing fence are opened read-only. A predecessor
    inbox without the coordinated fence cannot establish a safe online cut and
    is rejected instead of silently omitting its pending observations.
    """
    if conn.in_transaction:
        raise ValueError('Feedback backup requires an unpinned main connection')
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    if not database:
        raise ValueError('Feedback backup requires a file database')
    source = _queue(database)
    fence = source.path.with_name(source.path.name + '.lock')
    destination = (_queue(target) if backup_root is None else
                   _queue(Path(backup_root) / 'playback-feedback' / 'snapshot.db'))
    manifest = None
    try:
        if not fence.exists():
            _pin(conn)
            # If the first capture predates this pinned view, its inbox already
            # exists here. A later first capture belongs to the next backup.
            if source.path.exists():
                raise ValueError('Playback inbox has no coordinated backup fence; preserve it before recovery')
        else:
            with fence.open('rb') as stream:
                deadline = time.monotonic() + 5
                while True:
                    check_cancellable_work()
                    unlock = policy_documents._try_file_lock(stream, exclusive=True)
                    if unlock is not None:
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Playback feedback is busy; retry the backup')
                    time.sleep(.025)
                try:
                    if source.path.exists():
                        with closing(_reader(source.path)) as reader:
                            manifest = _copy(reader, destination)
                    # A replay can commit to main before waiting to acknowledge
                    # its inbox row. The retained receipt then removes that row
                    # without applying it twice after restoration.
                    _pin(conn)
                finally:
                    unlock()
        yield manifest
    finally:
        conn.rollback()


def restore(conn, manifest):
    """Restore the inbox into this disposable library without merging captures."""
    if (not isinstance(manifest, dict) or set(manifest) != {'format', 'database', 'sha256'}
            or manifest['format'] != 1 or not isinstance(manifest['database'], str)
            or not isinstance(manifest['sha256'], str)):
        raise ValueError('Invalid playback feedback backup manifest')
    snapshot = Path(manifest['database']).resolve(strict=True)
    with snapshot.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != manifest['sha256']:
            raise ValueError('Playback feedback backup does not match its digest')
    target = _queue(conn.execute('PRAGMA database_list').fetchone()[2])
    with closing(_reader(snapshot)) as reader:
        if [row[0] for row in reader.execute('PRAGMA integrity_check')] != ['ok']:
            raise ValueError('Playback feedback backup integrity check failed')
        if snapshot == target.path:
            for row in reader.execute('SELECT * FROM playback_feedback_inbox ORDER BY sequence'):
                Inbox.body(row)
            return
        _copy(reader, target)
