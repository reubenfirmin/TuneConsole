"""Snapshot playback captures together with the library's replay receipts."""
from contextlib import contextmanager, nullcontext
import hashlib
from pathlib import Path

from yt_playlist.core import fao, playback_inbox_file, playback_inbox_upgrade


def _database(conn):
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    if not path:
        raise ValueError('Feedback backup requires a file database')
    return Path(path).resolve()


def _copy(content, target):
    data = content.bytes
    playback_inbox_file.decode(data)
    with target.admission():
        if target.path.exists():
            raise FileExistsError(target.path)
        fao.write(target.path, fao.Content(data, metadata=content.info))
    return {'format': 2, 'file': str(target.path), 'sha256': hashlib.sha256(data).hexdigest()}


@contextmanager
def pinned(conn, target, *, backup_root=None):
    """Pin the main database after the queue snapshot, then release capture."""
    if conn.in_transaction:
        raise ValueError('Feedback backup requires an unpinned main connection')
    database = _database(conn)
    source = playback_inbox_file.inbox_path(database)
    if not source.exists() and playback_inbox_upgrade.legacy_path(database).exists():
        raise ValueError('Open the installed library to upgrade playback feedback before backup')
    destination = playback_inbox_file.inbox_path(target) if backup_root is None else Path(
        backup_root).resolve() / 'playback-feedback' / 'inbox.json'
    fence = source.with_suffix('.lock')
    lock = playback_inbox_file.fence(fence, read_only=True) if fence.exists() else nullcontext()
    try:
        with lock:
            manifest = _copy(fao.read(source), playback_inbox_file.InboxFile(destination)) if source.exists() else None
            conn.execute('BEGIN')
            conn.execute('SELECT rootpage FROM sqlite_schema LIMIT 1').fetchone()
        yield manifest
    finally:
        conn.rollback()


def restore(conn, manifest):
    """Restore an independently owned queue into this disposable library."""
    if not isinstance(manifest, dict) or not isinstance(manifest.get('sha256'), str):
        raise ValueError('Invalid playback feedback backup manifest')
    target = playback_inbox_file.InboxFile(playback_inbox_file.inbox_path(_database(conn)))
    if manifest.get('format') == 1 and isinstance(manifest.get('database'), str):
        snapshot = Path(manifest['database']).resolve(strict=True)
        with snapshot.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != manifest['sha256']:
                raise ValueError('Playback feedback backup does not match its digest')
        queue = playback_inbox_upgrade._read_installed(snapshot, playback_inbox_file.empty(), read_only=True)
        with target.admission():
            if target.path.exists():
                raise FileExistsError(target.path)
            target.write(queue)
        return
    if manifest.get('format') != 2 or not isinstance(manifest.get('file'), str):
        raise ValueError('Invalid playback feedback backup manifest')
    snapshot = Path(manifest['file']).resolve(strict=True)
    content = fao.read(snapshot, digest=manifest['sha256'])
    if snapshot == target.path:
        playback_inbox_file.decode(content.bytes)
    else:
        _copy(content, target)
