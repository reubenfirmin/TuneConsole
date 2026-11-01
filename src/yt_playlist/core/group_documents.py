"""Bounded immutable files directly owned by group versions and generations."""
import hashlib
import json
from itertools import chain
from pathlib import Path
import tempfile

from .evidence_ledger import canonical, _sync_directory
from . import policy_documents

FORMAT = 'musical-group-document-1'
KINDS = ('definition', 'generation', 'lineage')
PACK_BYTES = 256 * 1024
MAX_BYTES = 8 * 1024 * 1024
HEADER = b'TuneConsole musical groups 1\n'
BACKUP_FORMAT = 'musical-group-backup-1'
OWNERS = (('group_versions', ('id',), 'definition', 'definition'),
          ('group_generations', ('id',), 'metadata', 'generation'),
          ('group_lineage', ('source_alias', 'target_alias'), 'evidence', 'lineage'))


def root_for_database(path, memory_key):
    if path:
        database = Path(path).resolve()
        return database.with_name(database.name + '.groups')
    return Path(tempfile.gettempdir()) / 'tuneconsole-groups' / memory_key


def directory(conn, memory_key=None):
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    key = memory_key or getattr(conn, '_policy_memory_key', f'{id(conn)}')
    return root_for_database(path, key)


def publish_many(root, documents):
    """Yield filename/digest/size/offset for each bounded (kind, object) record."""
    records, used = [], len(HEADER)

    def flush():
        encoded = HEADER + b''.join(records)
        # This primitive only publishes immutable bytes under their SHA256. It
        # creates no shared registry or policy owner for these group files.
        filename = policy_documents._publish_encoded(root, encoded, hashlib.sha256(encoded).hexdigest())
        offset = len(HEADER)
        for record in records:
            yield filename, hashlib.sha256(record).hexdigest(), len(record), offset
            offset += len(record)

    for kind, value in documents:
        if kind not in KINDS or not isinstance(value, dict):
            raise ValueError('Invalid musical group document')
        encoded = canonical({'format': FORMAT, 'kind': kind, 'value': value})
        if len(encoded) > MAX_BYTES:
            raise ValueError('Musical group document exceeds its file byte budget')
        if records and used + len(encoded) > PACK_BYTES:
            yield from flush()
            records, used = [], len(HEADER)
        records.append(encoded)
        used += len(encoded)
    if records:
        yield from flush()


def read(filename, digest, size, offset, kind):
    if (type(size) is not int or not 0 < size <= MAX_BYTES or type(offset) is not int
            or offset < len(HEADER) or kind not in KINDS):
        raise ValueError('Invalid musical group file reference')
    with Path(filename).open('rb') as stream:
        if stream.read(len(HEADER)) != HEADER:
            raise ValueError('Invalid musical group pack')
        stream.seek(offset)
        encoded = stream.read(size)
    if len(encoded) != size or hashlib.sha256(encoded).hexdigest() != digest:
        raise ValueError('Musical group document does not match its digest')
    value = json.loads(encoded)
    if (not isinstance(value, dict) or set(value) != {'format', 'kind', 'value'}
            or value['format'] != FORMAT or value['kind'] != kind or not isinstance(value['value'], dict)
            or canonical(value) != encoded):
        raise ValueError('Unsupported musical group document')
    return value['value']


def references(conn):
    for table, keys, prefix, kind in OWNERS:
        fields = ','.join((*keys, prefix + '_filename', prefix + '_digest', prefix + '_bytes', prefix + '_offset'))
        for row in conn.execute(f'SELECT {fields} FROM {table} ORDER BY ' + ','.join(keys)):
            yield table, tuple(row[:len(keys)]), kind, *row[len(keys):]


def _manifest(root, value):
    encoded = canonical({'format': BACKUP_FORMAT, **value})
    if len(encoded) > MAX_BYTES:
        raise ValueError('Musical group backup manifest exceeds its byte budget')
    return policy_documents._publish_encoded(root, encoded, hashlib.sha256(encoded).hexdigest())


def _read_manifest(path):
    path = Path(path)
    with path.open('rb') as stream:
        encoded = stream.read(MAX_BYTES + 1)
    if len(encoded) > MAX_BYTES or hashlib.sha256(encoded).hexdigest() != path.stem:
        raise ValueError('Musical group backup manifest does not match its digest')
    value = json.loads(encoded)
    if (not isinstance(value, dict) or value.get('format') != BACKUP_FORMAT or canonical(value) != encoded):
        raise ValueError('Unsupported musical group backup manifest')
    return value


def _digest(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def export(conn, root):
    """Export exact owned ranges under the caller's pinned, retained snapshot."""
    if not conn.in_transaction:
        raise ValueError('Musical group export requires a pinned database snapshot')
    root = Path(root)
    if root.resolve() == directory(conn).resolve():
        raise ValueError('Musical group backup requires a separate directory')
    pending, entries, chunks, used = [], [], [], 0

    def copy_batch():
        values = ((kind, read(filename, digest, size, offset, kind))
                  for _table, _key, kind, filename, digest, size, offset in pending)
        for (table, key, _kind, _filename, digest, size, _offset), file in zip(pending, publish_many(root, values)):
            filename, copied, copied_size, offset = file
            if (copied, copied_size) != (digest, size):
                raise ValueError('Musical group backup changed its content')
            entries.append({'owner': table, 'key': list(key), 'digest': digest, 'byte_size': size,
                            'offset': offset, 'relative_filename': str(Path(filename).relative_to(root))})
            if len(entries) == 512:
                chunks.append(Path(_manifest(root, {'type': 'chunk', 'records': entries})).stem)
                entries.clear()

    for reference in references(conn):
        if pending and used + reference[-2] > PACK_BYTES:
            copy_batch()
            pending, used = [], 0
        pending.append(reference)
        used += reference[-2]
    if pending:
        copy_batch()
    if entries:
        chunks.append(Path(_manifest(root, {'type': 'chunk', 'records': entries})).stem)
    return _manifest(root, {'type': 'manifest', 'chunks': chunks})


def restore(conn, manifest):
    path = Path(manifest)
    document = _read_manifest(path)
    if (set(document) != {'format', 'type', 'chunks'} or document['type'] != 'manifest'
            or not isinstance(document['chunks'], list)):
        raise ValueError('Unsupported musical group backup manifest')
    entries = []
    for digest in document['chunks']:
        if not _digest(digest):
            raise ValueError('Invalid musical group backup chunk digest')
        chunk = _read_manifest(path.parent.parent / digest[:2] / (digest + '.json'))
        if (set(chunk) != {'format', 'type', 'records'} or chunk['type'] != 'chunk'
                or not isinstance(chunk['records'], list)):
            raise ValueError('Unsupported musical group backup chunk')
        entries.extend(chunk['records'])
    owners = {table: (keys, prefix, kind) for table, keys, prefix, kind in OWNERS}
    for entry in entries:
        if (not isinstance(entry, dict) or set(entry) != {
                'owner', 'key', 'digest', 'byte_size', 'offset', 'relative_filename'}
                or not isinstance(entry['owner'], str) or entry['owner'] not in owners
                or not isinstance(entry['key'], list) or not _digest(entry['digest'])
                or type(entry['byte_size']) is not int or not 0 < entry['byte_size'] <= MAX_BYTES
                or type(entry['offset']) is not int or entry['offset'] < len(HEADER)
                or not isinstance(entry['relative_filename'], str)):
            raise ValueError('Invalid musical group backup entry')
        expected_type = str if entry['owner'] == 'group_lineage' else int
        if (len(entry['key']) != len(owners[entry['owner']][0])
                or any(type(key) is not expected_type for key in entry['key'])):
            raise ValueError('Invalid musical group backup owner key')
        relative = Path(entry['relative_filename'])
        digest = relative.stem
        if (not _digest(digest) or relative != Path(digest[:2]) / (digest + '.json')
                or str(relative) != entry['relative_filename']):
            raise ValueError('Invalid musical group backup path')
    actual = {(entry['owner'], tuple(entry['key'])): (entry['digest'], entry['byte_size']) for entry in entries}
    if len(actual) != len(entries):
        raise ValueError('Duplicate musical group backup owner')
    if conn.in_transaction:
        raise ValueError('Musical group restore requires its own writer reservation')
    conn.execute('BEGIN IMMEDIATE')
    try:
        expected = {(table, key): (digest, size) for table, key, _kind, _filename, digest, size, _offset in references(conn)}
        if expected != actual:
            raise ValueError('Musical group backup does not match the restored database')
        values = ((owners[entry['owner']][2], read(path.parent.parent / entry['relative_filename'],
                   entry['digest'], entry['byte_size'], entry['offset'], owners[entry['owner']][2])) for entry in entries)
        targets = []
        for entry, file in zip(entries, publish_many(directory(conn), values)):
            filename, digest, size, offset = file
            if (digest, size) != (entry['digest'], entry['byte_size']):
                raise ValueError('Musical group restore changed its content')
            targets.append((entry['owner'], tuple(entry['key']), filename, offset))
        for table, key, filename, offset in targets:
            keys, prefix, _kind = owners[table]
            conn.execute(f'UPDATE {table} SET {prefix}_filename=?,{prefix}_offset=? WHERE '
                         + ' AND '.join(column + '=?' for column in keys), (filename, offset, *key))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def collect_orphans(conn):
    """Collect only files with no exact owner, while respecting old WAL readers."""
    if conn.in_transaction:
        raise ValueError('Musical group cleanup requires its own writer reservation')
    root = directory(conn)
    if not root.exists():
        return 0
    conn.execute('BEGIN IMMEDIATE')
    try:
        with (root / '.readers.lock').open('a+b') as stream:
            unlock = policy_documents._try_file_lock(stream, exclusive=True)
            if unlock is None:
                conn.rollback()
                return 0
            try:
                retained = {Path(reference[3]).resolve() for reference in references(conn)}
                removed = 0
                for path in chain(root.glob('*/*.json'), root.glob('*/.pending-*')):
                    if path.resolve() not in retained:
                        path.unlink()
                        _sync_directory(path.parent)
                        removed += 1
                conn.commit()
                return removed
            finally:
                unlock()
    except BaseException:
        conn.rollback()
        raise
