"""Bounded immutable files directly owned by group versions and generations."""
import hashlib
import json
from contextlib import ExitStack, contextmanager
from itertools import chain
from pathlib import Path
import tempfile

from .evidence_ledger import canonical
from . import policy_documents, fao

FORMAT = 'musical-group-document-1'
KINDS = ('definition', 'generation', 'lineage')
PACK_BYTES = 256 * 1024
MAX_BYTES = 8 * 1024 * 1024
HEADER = b'TuneConsole musical groups 1\n'
BACKUP_FORMAT = 'musical-group-backup-1'
OWNERS = (('group_versions', ('id',), 'definition', 'definition'),
          ('group_generations', ('id',), 'metadata', 'generation'),
          ('group_lineage', ('source_alias', 'target_alias'), 'evidence', 'lineage'))


class GroupFiles:
    def __init__(self, root, policy_root):
        self.root, self.policy_root = root, policy_root

    @contextmanager
    def retained(self):
        with ExitStack() as readers:
            readers.enter_context(policy_documents.retained_reader(self.root))
            readers.enter_context(policy_documents.retained_reader(self.policy_root))
            yield

    def read(self, row, kind):
        prefix = {'generation': 'metadata', 'definition': 'definition', 'lineage': 'evidence'}[kind]
        return read(*(row[prefix + '_' + field] for field in ('filename', 'digest', 'bytes', 'offset')), kind)

    def write(self, documents):
        return list(publish_many(self.root, documents))

    def write_policy(self, value):
        return next(policy_documents.publish_many(self.policy_root, [value]))

    def policy(self, reference):
        if reference is None:
            raise ValueError('Missing retained musical group policy')
        filename, digest, base, offset, size = reference['variant']
        result = policy_documents.read(filename, digest, offset=offset, size=size)
        if result.pop(policy_documents.BASE_FIELD, None) != base:
            raise ValueError('Policy configuration does not match its shared-body reference')
        if base is not None:
            if reference['body'] is None:
                raise ValueError(f'Missing shared recommendation policy body: {base}')
            filename, digest, offset, size = reference['body']
            result = {**policy_documents.read(filename, digest, offset=offset, size=size), **result}
        return result


def root_for_database(path, memory_key):
    if path:
        database = Path(path).resolve()
        return database.with_name(database.name + '.groups')
    return Path(tempfile.gettempdir()) / 'tuneconsole-groups' / memory_key


def directory(conn, memory_key=None):
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    key = memory_key or getattr(conn, '_policy_memory_key', f'{id(conn)}')
    return root_for_database(path, key)


def publish_many(root, documents, *, age=None):
    """Yield filename/digest/size/offset for each bounded (kind, object) record."""
    def encoded_records():
        for kind, value in documents:
            if kind not in KINDS or not isinstance(value, dict):
                raise ValueError('Invalid musical group document')
            encoded = canonical({'format': FORMAT, 'kind': kind, 'value': value})
            if len(encoded) > MAX_BYTES:
                raise ValueError('Musical group document exceeds its file byte budget')
            yield encoded

    yield from policy_documents.pack_encoded(root, encoded_records(),
        header=HEADER, pack_bytes=PACK_BYTES, age=age)


def read(filename, digest, size, offset, kind, *, with_age=False):
    if (type(size) is not int or not 0 < size <= MAX_BYTES or type(offset) is not int
            or offset < len(HEADER) or kind not in KINDS):
        raise ValueError('Invalid musical group file reference')
    with fao.read(filename) as stream:
        age = stream.info
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
    return (value['value'], age) if with_age else value['value']


def references(conn):
    for table, keys, prefix, kind in OWNERS:
        fields = ','.join((*keys, prefix + '_filename', prefix + '_digest', prefix + '_bytes', prefix + '_offset'))
        for row in conn.execute(f'SELECT {fields} FROM {table} ORDER BY {prefix}_filename,{prefix}_offset,' + ','.join(keys)):
            yield table, tuple(row[:len(keys)]), kind, *row[len(keys):]


def repack_batch(conn, table, after=None, limit=128):
    """Repack a bounded owner prefix within the caller's bookmarked transaction.

    Return (last key, row count, complete). Files are immutable and published
    before their SQL references; a failed batch leaves only collectible files.
    """
    if not conn.in_transaction or type(limit) is not int or not 0 < limit <= 128:
        raise ValueError('Musical group repacking needs a bounded writer transaction')
    keys, prefix, kind = next((keys, prefix, kind) for owner, keys, prefix, kind in OWNERS if owner == table)
    fields = ','.join((*keys, prefix + '_filename', prefix + '_digest', prefix + '_bytes', prefix + '_offset'))
    predicate = ' WHERE (' + ','.join(keys) + ')>(' + ','.join('?' for _ in keys) + ')' if after is not None else ''
    rows = conn.execute('SELECT ' + fields + ' FROM ' + table + predicate + ' ORDER BY ' + ','.join(keys) +
                        ' LIMIT ?', (*after, limit) if after is not None else (limit,)).fetchall()
    pending, used = [], 0
    for row in rows:
        if pending and used + row[-2] > PACK_BYTES:
            break
        pending.append(row)
        used += row[-2]
    values = (((kind, value), age) for row in pending
              for value, age in [read(*row[len(keys):], kind, with_age=True)])
    for row, (filename, digest, size, offset) in zip(pending, policy_documents.repack(directory(conn), values, publish_many)):
        if (digest, size) != (row[-3], row[-2]):
            raise ValueError('Musical group repacking changed its content')
        conn.execute('UPDATE ' + table + ' SET ' + prefix + '_filename=?,' + prefix + '_offset=? WHERE ' +
                     ' AND '.join(column + '=?' for column in keys), (filename, offset, *row[:len(keys)]))
    return (tuple(pending[-1][:len(keys)]) if pending else after), len(pending), len(rows) < limit and len(pending) == len(rows)


def _manifest(root, value):
    encoded = canonical({'format': BACKUP_FORMAT, **value})
    if len(encoded) > MAX_BYTES:
        raise ValueError('Musical group backup manifest exceeds its byte budget')
    return policy_documents._publish_encoded(root, encoded, hashlib.sha256(encoded).hexdigest())


def _read_manifest(path):
    path = Path(path)
    with fao.read(path) as stream:
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
        values = (((kind, value), age)
                  for _table, _key, kind, filename, digest, size, offset in pending
                  for value, age in [read(filename, digest, size, offset, kind, with_age=True)])
        for (table, key, _kind, _filename, digest, size, _offset), file in zip(
                pending, policy_documents.repack(root, values, publish_many)):
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
        values = (((owners[entry['owner']][2], value), age) for entry in entries
                  for value, age in [read(path.parent.parent / entry['relative_filename'],
                    entry['digest'], entry['byte_size'], entry['offset'], owners[entry['owner']][2], with_age=True)])
        targets = []
        for entry, file in zip(entries, policy_documents.repack(directory(conn), values, publish_many)):
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
                        removed += fao.delete(path, missing_ok=True)
                conn.commit()
                return removed
            finally:
                unlock()
    except BaseException:
        conn.rollback()
        raise
