"""Immutable sample JSON files; SQLite retains only their collection identity."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from itertools import zip_longest
from urllib.parse import quote

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.evidence_ledger import canonical
from . import fao, file_collection

MAX_BYTES = 256 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
REFERENCES = ('SELECT id,recording_id,video_id,run_id,point,captured_at,filename '
              'FROM sample_attributes ORDER BY id')


def directory(conn, memory_key=None):
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    return root_for_database(path, memory_key)


def root_for_database(path, memory_key=None):
    if path:
        return Path(path).resolve().parent / 'sample-attributes'
    if memory_key is None:
        return Path(tempfile.mkdtemp(prefix='tuneconsole-samples-'))
    return Path(tempfile.gettempdir()) / 'tuneconsole-samples' / memory_key


class SampleFiles:
    def __init__(self, root):
        self.root = root

    def retained(self):
        return file_collection.retained_reader(self.root)

    def read(self, reference):
        return read(reference['filename'], expected={name: reference[name]
                    for name in ('id', 'run_id', 'point', 'video_id')},
                    digest=reference.get('content_digest'))

    def reference(self, sample):
        encoded = _capture_bytes(sample)
        return dict(filename=str(_record_path(self.root, sample['id'])),
                    content_digest=hashlib.sha256(encoded).hexdigest())

    def matches_reference(self, reference, sample):
        digest = reference.get('content_digest') or Path(reference['filename']).stem
        return digest == hashlib.sha256(_capture_bytes(sample)).hexdigest()

    def publish_reserved(self, reference, sample):
        encoded = _capture_bytes(sample)
        if not self.matches_reference(reference, sample):
            raise ValueError('Sample file does not match its reserved content')
        fao.write(reference['filename'], fao.Content(encoded, max_bytes=MAX_BYTES))

def publish(root, sample):
    """Retain the historical conversion writer and portable backup format."""
    encoded = _capture_bytes(sample)
    return _publish(root, encoded)


def _capture_bytes(sample):
    encoded = canonical(sample)
    if len(encoded) > MAX_BYTES:
        raise ValueError('Sample file exceeds the input byte budget')
    return encoded


def _record_path(root, sample_id):
    if not isinstance(sample_id, str):
        raise ValueError('Invalid sample file identity')
    identity = quote(sample_id, safe='').replace('.', '%2E')
    if not identity or len(identity) > 240:
        raise ValueError('Invalid sample file identity')
    shard = sample_id.encode('utf-8')[:1].hex()
    return Path(root) / 'by-id' / shard / (identity + '.json')


def _publish_record(root, sample_id, encoded, *, age=None):
    path = _record_path(root, sample_id)
    fao.write(path, fao.Content(encoded, metadata=age, max_bytes=MAX_BYTES))
    return str(path)


def _publish(root, encoded, *, age=None):
    # Content addressing allows independent databases in a directory and makes
    # interrupted migrations/retries reuse an already durable file safely.
    digest = hashlib.sha256(encoded).hexdigest()
    path = Path(root) / digest[:2] / (digest + '.json')
    fao.write(path, fao.Content(encoded, metadata=age))
    return str(path)


def _encoded(filename, *, max_bytes=MAX_BYTES, with_age=False, digest=None):
    """Verify bounded bytes and retained checksums, including old hash names."""
    path = Path(filename)
    with fao.read(path) as stream:
        encoded = stream.read(max_bytes + 1)
        age = stream.info
    if len(encoded) > max_bytes:
        raise ValueError('Sample file exceeds the input byte budget')
    required = digest or (path.stem if 'by-id' not in path.parts else None)
    if required is not None and required != hashlib.sha256(encoded).hexdigest():
        raise ValueError('Sample file does not match its content hash')
    return (encoded, age) if with_age else encoded


def _sample(encoded, expected):
    sample = json.loads(encoded)
    if not isinstance(sample, dict) or any(sample.get(key) != value for key, value in (expected or {}).items()):
        raise ValueError('Sample file does not match its collection identity')
    return sample


def read(filename, *, expected=None, max_bytes=MAX_BYTES, digest=None):
    """Bound external input and verify bytes and collection identity on every read."""
    sample = _sample(_encoded(filename, max_bytes=max_bytes, digest=digest), expected)
    path = Path(filename)
    if 'by-id' in path.parts and (not isinstance(sample.get('id'), str)
                                 or quote(sample['id'], safe='').replace('.', '%2E') != path.stem):
        raise ValueError('Sample file does not match its collection identity')
    return sample


def _identity(row):
    return dict(zip(('id', 'run_id', 'point', 'video_id'), (row[0], row[3], row[4], row[2])))


def _manifest(root, document):
    encoded = canonical(document)
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise ValueError('Sample backup manifest exceeds the input byte budget')
    return _publish(root, encoded)


def _references(conn):
    columns = {row[1] for row in conn.execute('PRAGMA main.table_info(sample_attributes)')}
    native = 'file_status' in columns
    extra = ',content_digest,file_status' if native else ",NULL,'ready'"
    query = REFERENCES.replace(' FROM sample_attributes', extra + ' FROM sample_attributes')
    for row in conn.execute(query):
        yield tuple(row[:7]), row[7], row[8]


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
    for row, digest, status in _references(conn):
        check_cancellable_work()
        present = True
        try:
            encoded, age = _encoded(row[-1], digest=digest, with_age=True)
        except FileNotFoundError:
            if status != 'pending':
                raise
            present = False
        if present:
            _sample(encoded, _identity(row))
            filename = _publish(root, encoded, age=age)
            digest = Path(filename).stem
        entries.append({'identity': list(row[:-1]), 'digest': digest,
                        'file_status': status, 'present': present})
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
            native = 'file_status' in {row[1] for row in conn.execute('PRAGMA main.table_info(sample_attributes)')}
            for reference, entry in zip_longest(_references(conn), _entries(manifest)):
                check_cancellable_work()
                row, original_digest, status = reference if reference is not None else (None, None, None)
                if row is None or not isinstance(entry, dict) or entry.get('identity') != list(row[:-1]):
                    raise ValueError('Sample backup does not match the restored database')
                digest = entry.get('digest')
                if (not isinstance(digest, str) or len(digest) != 64
                        or any(c not in '0123456789abcdef' for c in digest)
                        or digest != (original_digest or Path(row[-1]).stem)
                        or entry.get('file_status', 'ready') != status
                        or type(entry.get('present', True)) is not bool):
                    raise ValueError('Sample backup identity mismatch')
                if not entry.get('present', True):
                    if status != 'pending':
                        raise ValueError('Sample backup lacks completed evidence')
                    filename = str(_record_path(destination, row[0]))
                    conn.execute('UPDATE sample_attributes SET filename=? WHERE id=?', (filename, row[0]))
                    continue
                encoded, age = _encoded(root / digest[:2] / (digest + '.json'), with_age=True)
                _sample(encoded, _identity(row))
                filename = (_publish_record(destination, row[0], encoded, age=age) if native else
                            _publish(destination, encoded, age=age))
                if native:
                    conn.execute('UPDATE sample_attributes SET filename=?,content_digest=? WHERE id=?',
                                 (filename, digest, row[0]))
                else:
                    conn.execute('UPDATE sample_attributes SET filename=? WHERE id=?', (filename, row[0]))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def recording(conn, sample, now):
    from .sample_identity import recording as admit
    return admit(conn, sample, now)
