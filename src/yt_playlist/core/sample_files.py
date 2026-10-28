"""Immutable sample JSON files; SQLite retains only their collection identity."""
import hashlib
import json
import os
from pathlib import Path
import tempfile

from yt_playlist.core.evidence_ledger import _mkdir, _sync_directory, canonical


def directory(conn, memory_key=None):
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    if path:
        return Path(path).resolve().parent / 'sample-attributes'
    if memory_key is None:
        return Path(tempfile.mkdtemp(prefix='tuneconsole-samples-'))
    return Path(tempfile.gettempdir()) / 'tuneconsole-samples' / memory_key


def publish(root, sample):
    """Fsync before returning a filename; retries cannot replace existing evidence."""
    encoded = canonical(sample)
    # Content addressing allows independent databases in a directory and makes
    # interrupted migrations/retries reuse an already durable file safely.
    digest = hashlib.sha256(encoded).hexdigest()
    path = Path(root) / digest[:2] / (digest + '.json')
    _mkdir(path.parent)
    pending = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.pending-', delete=False) as stream:
            pending = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(pending, path)
        except FileExistsError:
            if path.read_bytes() != encoded:
                raise ValueError('Sample file does not match its content hash')
        _sync_directory(path.parent)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)
    return str(path)


def read(filename, *, expected=None, max_bytes=256 * 1024):
    """Bound external input and verify the immutable content address on every read."""
    path = Path(filename)
    with path.open('rb') as stream:
        encoded = stream.read(max_bytes + 1)
    if len(encoded) > max_bytes:
        raise ValueError('Sample file exceeds the input byte budget')
    if path.stem != hashlib.sha256(encoded).hexdigest():
        raise ValueError('Sample file does not match its content hash')
    sample = json.loads(encoded)
    if not isinstance(sample, dict) or any(sample.get(key) != value for key, value in (expected or {}).items()):
        raise ValueError('Sample file does not match its collection identity')
    return sample


def recording(conn, sample, now):
    """An unknown exact video gets its own provisional identity, never a text match."""
    row = conn.execute('SELECT recording_id FROM youtube_items WHERE video_id=?',
                       (sample['video_id'],)).fetchone()
    if row:
        return row[0]
    # Normal ingestion also registers title/artist aliases, which may rename
    # existing ambiguous identities. Collection and migration must leave those
    # identities (and their historical keyed state) alone.
    rid = conn.execute('INSERT INTO recordings(identity_key,title,artist,created_at) VALUES (?,?,?,?)',
        ('@sample-video:' + sample['video_id'], sample.get('title'), sample.get('artist'), now)).lastrowid
    conn.execute('INSERT INTO youtube_items(video_id,recording_id,title,artist,orig_title,orig_artist) '
        'VALUES (?,?,?,?,?,?)', (sample['video_id'], rid, sample.get('title'), sample.get('artist'),
                               sample.get('title'), sample.get('artist')))
    conn.execute('INSERT INTO catalog_entries(recording_id,video_id) VALUES (?,?)', (rid, sample['video_id']))
    conn.execute('INSERT INTO item_recording_associations(video_id,recording_id,source,evidence,at) '
        "VALUES (?,?,'sample','{}',?)", (sample['video_id'], rid, now))
    return rid
