"""Immutable sample JSON files; SQLite retains only their collection identity."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from itertools import zip_longest

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.evidence_ledger import canonical
from . import fao

MAX_BYTES = 256 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
REFERENCES = ('SELECT id,recording_id,video_id,run_id,point,captured_at,filename '
              'FROM sample_attributes ORDER BY id')


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
    if len(encoded) > MAX_BYTES:
        raise ValueError('Sample file exceeds the input byte budget')
    return _publish(root, encoded)


def _publish(root, encoded, *, age=None):
    # Content addressing allows independent databases in a directory and makes
    # interrupted migrations/retries reuse an already durable file safely.
    digest = hashlib.sha256(encoded).hexdigest()
    path = Path(root) / digest[:2] / (digest + '.json')
    fao.write(path, fao.Content(encoded, metadata=age))
    return str(path)


def _encoded(filename, *, max_bytes=MAX_BYTES, with_age=False):
    """Bound external input and verify the immutable content address on every read."""
    path = Path(filename)
    with fao.read(path) as stream:
        encoded = stream.read(max_bytes + 1)
        age = stream.info
    if len(encoded) > max_bytes:
        raise ValueError('Sample file exceeds the input byte budget')
    if path.stem != hashlib.sha256(encoded).hexdigest():
        raise ValueError('Sample file does not match its content hash')
    return (encoded, age) if with_age else encoded


def _sample(encoded, expected):
    sample = json.loads(encoded)
    if not isinstance(sample, dict) or any(sample.get(key) != value for key, value in (expected or {}).items()):
        raise ValueError('Sample file does not match its collection identity')
    return sample


def read(filename, *, expected=None, max_bytes=MAX_BYTES):
    """Bound external input and verify bytes and collection identity on every read."""
    return _sample(_encoded(filename, max_bytes=max_bytes), expected)


def _identity(row):
    return dict(zip(('id', 'run_id', 'point', 'video_id'), (row[0], row[3], row[4], row[2])))


def _manifest(root, document):
    encoded = canonical(document)
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise ValueError('Sample backup manifest exceeds the input byte budget')
    return _publish(root, encoded)


def export(conn, root):
    """Export exact captured bytes from the caller's pinned SQL/file snapshot."""
    if not conn.in_transaction:
        raise ValueError('Sample export requires a pinned database snapshot')
    root = Path(root)
    if root.resolve() == directory(conn).resolve():
        raise ValueError('Sample backup requires a separate destination directory')
    chunks, entries = [], []
    def flush():
        filename = _manifest(root, {'format': 1, 'type': 'sample-backup-chunk', 'files': entries})
        chunks.append(Path(filename).stem)
        # Bound the final manifest as well as each independently read chunk.
        if len(chunks) * 67 > MAX_MANIFEST_BYTES - 128:
            raise ValueError('Sample backup manifest exceeds the input byte budget')
        entries.clear()
    for row in conn.execute(REFERENCES):
        check_cancellable_work()
        encoded, age = _encoded(row[-1], with_age=True)
        _sample(encoded, _identity(row))
        filename = _publish(root, encoded, age=age)
        entries.append({'identity': list(row[:-1]), 'digest': Path(filename).stem})
        if len(entries) == 256:
            flush()
    if entries:
        flush()
    return _manifest(root, {'format': 1, 'type': 'sample-backup', 'chunks': chunks})


def _entries(manifest):
    path = Path(manifest)
    document = read(path, max_bytes=MAX_MANIFEST_BYTES,
                    expected={'format': 1, 'type': 'sample-backup'})
    chunks = document.get('chunks')
    if (not isinstance(chunks, list) or any(not isinstance(value, str) for value in chunks)
            or len(set(chunks)) != len(chunks)):
        raise ValueError('Invalid sample backup chunks')
    root = path.parent.parent
    for digest in chunks:
        check_cancellable_work()
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('Invalid sample backup digest')
        chunk = read(root / digest[:2] / (digest + '.json'), max_bytes=MAX_MANIFEST_BYTES,
                     expected={'format': 1, 'type': 'sample-backup-chunk'})
        entries = chunk.get('files')
        if not isinstance(entries, list) or not 0 < len(entries) <= 256:
            raise ValueError('Invalid sample backup entries')
        yield from entries


def restore(conn, manifest, *, memory_key=None):
    """Repoint only this copy after all bytes and SQL identities agree; retry safely."""
    from .policy_documents import coordinate_updates
    if conn.in_transaction:
        raise ValueError('Sample restore requires its own writer reservation')
    root = Path(manifest).parent.parent
    destination = directory(conn, memory_key)
    if root.resolve() == destination.resolve():
        raise ValueError('Sample restore requires independent destination custody')
    conn.execute('BEGIN IMMEDIATE')
    try:
        with coordinate_updates(conn, ['sample_attributes']):
            for row, entry in zip_longest(conn.execute(REFERENCES), _entries(manifest)):
                check_cancellable_work()
                if row is None or not isinstance(entry, dict) or entry.get('identity') != list(row[:-1]):
                    raise ValueError('Sample backup does not match the restored database')
                digest = entry.get('digest')
                if (not isinstance(digest, str) or len(digest) != 64
                        or any(c not in '0123456789abcdef' for c in digest)
                        or digest != Path(row[-1]).stem):
                    raise ValueError('Sample backup identity mismatch')
                encoded, age = _encoded(root / digest[:2] / (digest + '.json'), with_age=True)
                _sample(encoded, _identity(row))
                filename = _publish(destination, encoded, age=age)
                conn.execute('UPDATE sample_attributes SET filename=? WHERE id=?', (filename, row[0]))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


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
