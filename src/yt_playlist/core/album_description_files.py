"""Immutable paid description bodies, directly owned by requests and results."""
import hashlib
import json
import os
from pathlib import Path
import tempfile

from .evidence_ledger import canonical
from .description_paths import record_id, record_path
from . import fao

FORMAT = 'album-description-1'
MAX_BYTES = 256 * 1024
OWNERS = {
    'album_description_requests': (('id',), 'outcome_filename', 'outcome_digest'),
    'album_descriptions': (('id',), 'filename', 'content_digest'),
    'legacy_album_description_usage': (('id',), 'filename', 'content_digest'),
    'album_description_access': (('provider', 'scope', 'credential_key'), 'evidence_filename', 'evidence_digest'),
}


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


def describe(raw):
    encoded = document(raw)
    return hashlib.sha256(encoded).hexdigest(), len(encoded)


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


def _references(conn):
    for owner, (keys, filename, digest) in OWNERS.items():
        fields = ','.join((*keys, filename, digest))
        for row in conn.execute(f'SELECT {fields} FROM {owner} WHERE {filename} IS NOT NULL ORDER BY '
                                + ','.join(keys)):
            yield owner, tuple(row[:len(keys)]), row[-2], row[-1]


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


def _key(owner, value):
    if (not isinstance(value, list) or len(value) != len(OWNERS[owner][0])
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
    entries = {}
    for digest in value['chunks']:
        _digest(digest)
        chunk = json.loads(read(root / digest[:2] / (digest + '.json'), digest))
        if (not isinstance(chunk, dict) or set(chunk) != {'type', 'entries'}
                or chunk['type'] != 'backup-chunk' or not isinstance(chunk['entries'], list)):
            raise ValueError('Unsupported paid description backup chunk')
        for entry in chunk['entries']:
            if (not isinstance(entry, dict) or set(entry) != {'owner', 'key', 'digest', 'relative_filename'}
                    or entry['owner'] not in OWNERS or not isinstance(entry['key'], list)
                    or not isinstance(entry['relative_filename'], str)
                    or len(entry['key']) != len(OWNERS[entry['owner']][0])):
                raise ValueError('Invalid paid description backup entry')
            digest = _digest(entry['digest'])
            relative = Path(entry['relative_filename'])
            identity = record_id(relative)
            expected_path = (Path(digest[:2]) / (digest + '.json') if identity is None
                             else record_path(Path(), identity))
            if relative != expected_path:
                raise ValueError('Invalid paid description backup path')
            identity = (entry['owner'], _key(entry['owner'], entry['key']))
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
            columns, field, _digest_column = OWNERS[owner]
            conn.execute(f'UPDATE {owner} SET {field}=? WHERE ' + ' AND '.join(f'{column}=?' for column in columns),
                         (filename, *key))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
