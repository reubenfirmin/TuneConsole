"""Immutable files for retained provider evidence and installed paid-result migration."""
import hashlib
import json
import os
from pathlib import Path
import tempfile

from .evidence_ledger import canonical
from .description_paths import record_id, record_path
from . import fao, paid_description_access

FORMAT = 'album-description-1'
MAX_BYTES = 256 * 1024
USAGE_RECORD = 'provider_usage'  # Stable backup manifest type.


def directory(conn, memory_key=None):
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    memory_key = memory_key or getattr(conn, '_policy_memory_key', f'{os.getpid()}-{id(conn)}')
    return root_for_database(path, memory_key)


def root_for_database(path, memory_key):
    if path:
        database = Path(path).resolve()
        return database.with_name(database.name + '.descriptions')
    return Path(tempfile.gettempdir()) / 'tuneconsole-descriptions' / memory_key


def document(raw):
    """Retain original JSON text as evidence, including its exact formatting."""
    if not isinstance(raw, str) or not isinstance(json.loads(raw), dict):
        raise ValueError('Album description result must be a JSON object')
    # Validate finite JSON values before any filesystem publication.
    canonical(json.loads(raw))
    encoded = canonical({'format': FORMAT, 'record_json': raw})
    if len(encoded) > MAX_BYTES:
        raise ValueError('Album description result exceeds its file byte budget')
    return encoded


def publish(root, raw, *, age=None):
    encoded = document(raw)
    digest = hashlib.sha256(encoded).hexdigest()
    path = Path(root) / digest[:2] / (digest + '.json')
    fao.write(path, fao.Content(encoded, metadata=age, max_bytes=MAX_BYTES))
    return str(path), digest, len(encoded)


def _existing(path, *, with_age=False):
    with fao.read(path) as stream:
        encoded = stream.read(MAX_BYTES + 1)
        age = stream.info
    return (encoded, age) if with_age else encoded


def publish_reserved(root, identity, raw, *, age=None):
    encoded = document(raw)
    path = record_path(root, identity)
    with fao.prepare(path, fao.Content(encoded, metadata=age, max_bytes=MAX_BYTES)) as prepared:
        prepared.publish()
    return str(path), hashlib.sha256(encoded).hexdigest(), len(encoded)


def read(filename, digest, *, with_age=False):
    encoded, age = _existing(filename, with_age=True)
    if (len(encoded) > MAX_BYTES or hashlib.sha256(encoded).hexdigest() != digest
            or (record_id(filename) is None and Path(filename).stem != digest)):
        raise ValueError('Album description file does not match its digest')
    value = json.loads(encoded)
    if (not isinstance(value, dict) or set(value) != {'format', 'record_json'}
            or value['format'] != FORMAT or document(value['record_json']) != encoded):
        raise ValueError('Unsupported album description file')
    return (value['record_json'], age) if with_age else value['record_json']


def _usage_owner(conn):
    # Snapshot tools also export and restore a database before opening it.
    previous = conn.execute("SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name=?", (USAGE_RECORD,))
    return (USAGE_RECORD, 'id') if previous.fetchone() else ('album_description_requests', 'usage_id')


def _references(conn):
    table, key = _usage_owner(conn)
    for identity, filename, digest in conn.execute(
            f'SELECT {key},evidence_filename,evidence_digest FROM {table} '
            f'WHERE evidence_filename IS NOT NULL ORDER BY {key}'):
        yield USAGE_RECORD, (identity,), filename, digest
    for key, filename, digest in paid_description_access.receipts(conn):
        # Preserve the manifest's original owner label across the storage change.
        yield 'settings', (key,), filename, digest


def export(conn, root):
    """Export every retained body from the same pinned snapshot as the DB backup."""
    if not conn.in_transaction:
        raise ValueError('Paid description export requires a pinned database snapshot')
    root = Path(root).resolve()
    if root == directory(conn).resolve():
        raise ValueError('Paid description backup requires a separate directory')
    entries, chunks = [], []
    for owner, key, filename, digest in _references(conn):
        value, age = read(filename, digest, with_age=True)
        identity = record_id(filename)
        target, copied, _size = (publish(root, value, age=age) if identity is None
                                else publish_reserved(root, identity, value, age=age))
        if copied != digest:
            raise ValueError('Paid description backup changed original evidence')
        entries.append({'owner': owner, 'key': key, 'digest': digest,
                        'relative_filename': str(Path(target).relative_to(root))})
        if len(entries) == 512:
            chunks.append(publish(root, json.dumps({'type': 'backup-chunk', 'entries': entries}))[1])
            entries = []
    if entries:
        chunks.append(publish(root, json.dumps({'type': 'backup-chunk', 'entries': entries}))[1])
    return publish(root, json.dumps({'type': 'backup-manifest', 'chunks': chunks}))[0]


def _digest(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError('Invalid paid description backup digest')
    return value


def _key(columns, value):
    if (not isinstance(value, list) or len(value) != len(columns)
            or any(type(part) not in (int, str) for part in value)):
        raise ValueError('Invalid paid description backup owner key')
    return tuple(value)


def restore(conn, manifest, *, memory_key=None):
    """Publish every file before repointing a restored DB; retain all request IDs."""
    path = Path(manifest)
    root = path.parent.parent
    value = json.loads(read(path, _digest(path.stem)))
    if (not isinstance(value, dict) or set(value) != {'type', 'chunks'}
            or value['type'] != 'backup-manifest' or not isinstance(value['chunks'], list)):
        raise ValueError('Unsupported paid description backup manifest')
    keys = {USAGE_RECORD: ('usage_id',), 'settings': ('key',)}
    usage_table, usage_key = _usage_owner(conn)
    entries = {}
    for digest in value['chunks']:
        _digest(digest)
        chunk = json.loads(read(root / digest[:2] / (digest + '.json'), digest))
        if (not isinstance(chunk, dict) or set(chunk) != {'type', 'entries'}
                or chunk['type'] != 'backup-chunk' or not isinstance(chunk['entries'], list)):
            raise ValueError('Unsupported paid description backup chunk')
        for entry in chunk['entries']:
            if (not isinstance(entry, dict) or set(entry) != {'owner', 'key', 'digest', 'relative_filename'}
                    or entry['owner'] not in keys or not isinstance(entry['relative_filename'], str)):
                raise ValueError('Invalid paid description backup entry')
            digest = _digest(entry['digest'])
            relative = Path(entry['relative_filename'])
            identity = record_id(relative)
            expected_path = (Path(digest[:2]) / (digest + '.json') if identity is None
                             else record_path(Path(), identity))
            if relative != expected_path:
                raise ValueError('Invalid paid description backup path')
            identity = (entry['owner'], _key(keys[entry['owner']], entry['key']))
            if identity in entries:
                raise ValueError('Duplicate paid description in backup manifest')
            entries[identity] = entry
    if conn.in_transaction:
        raise ValueError('Paid description restore requires its own writer reservation')
    conn.execute('BEGIN IMMEDIATE')
    try:
        expected = {(owner, key): (digest, record_id(filename))
                    for owner, key, filename, digest in _references(conn)}
        if expected != {identity: (entry['digest'], record_id(entry['relative_filename']))
                        for identity, entry in entries.items()}:
            raise ValueError('Paid description backup does not match the restored database')
        targets = []
        for (owner, key), entry in entries.items():
            value, age = read(root / entry['relative_filename'], entry['digest'], with_age=True)
            identity = record_id(entry['relative_filename'])
            target = directory(conn, memory_key)
            filename, copied, _size = (publish(target, value, age=age) if identity is None
                                      else publish_reserved(target, identity, value, age=age))
            if copied != entry['digest']:
                raise ValueError('Paid description restore changed original evidence')
            targets.append((owner, key, filename))
        for owner, key, filename in targets:
            if owner == 'settings':
                paid_description_access.rebase_receipt(conn, key[0], filename)
            else:
                conn.execute(f'UPDATE {usage_table} SET evidence_filename=? WHERE {usage_key}=?',
                             (filename, *key))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
