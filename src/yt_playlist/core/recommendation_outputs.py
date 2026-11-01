"""Bounded opaque output files owned directly by recommendation publications."""
import hashlib
import json
import math
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
import tempfile

from . import policy_documents, fao
from .activity import check_cancellable_work
from .evidence_ledger import canonical, _sync_directory

FORMAT = 'prepared-recommendation-output-1'
BACKUP_FORMAT = 'prepared-recommendation-backup-1'
MAX_BYTES = 1024 * 1024
MANIFEST_BYTES = 8 * 1024 * 1024
OUTPUT_OWNERS = ('prepared_outputs', 'album_note_outputs', 'listening_output_files', 'home_display_files',
                 'recommendation_report_files', 'model_pair_files', 'radio_seed_output', 'model_autotune_output')


def decode_json(raw):
    """Validate opaque JSON without silently admitting non-finite numbers."""
    def invalid(value):
        raise ValueError('Non-finite number in prepared output')
    def number(value):
        parsed = float(value)
        return parsed if math.isfinite(parsed) else invalid(value)
    return json.loads(raw, parse_float=number, parse_constant=invalid)


def root_for_database(path, memory_key):
    if path:
        database = Path(path).resolve()
        return database.with_name(database.name + '.recommendations')
    return Path(tempfile.gettempdir()) / 'tuneconsole-recommendations' / memory_key


def directory(conn):
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    return root_for_database(path, getattr(conn, '_policy_memory_key', str(id(conn))))


def encode(surface, chunk_key, value):
    if (not isinstance(surface, str) or not surface or len(surface.encode()) > 1024
            or not isinstance(chunk_key, str) or len(chunk_key.encode()) > 1024):
        raise ValueError('Invalid prepared output owner')
    encoded = canonical({'format': FORMAT, 'surface': surface, 'chunk_key': chunk_key, 'output': value})
    if len(encoded) > MAX_BYTES:
        raise ValueError('Prepared output exceeds its chunk byte budget')
    return encoded


def publish(root, encoded):
    if not isinstance(encoded, bytes) or len(encoded) > MAX_BYTES:
        raise ValueError('Invalid prepared output bytes')
    digest = hashlib.sha256(encoded).hexdigest()
    # Validate the exact bytes before exposing a durable path to an SQL owner.
    _decode(encoded)
    return policy_documents._publish_encoded(root, encoded, digest), digest, len(encoded)


def _decode(encoded, surface=None, chunk_key=None):
    document = json.loads(encoded)
    if (not isinstance(document, dict) or set(document) != {'format', 'surface', 'chunk_key', 'output'}
            or document['format'] != FORMAT or not isinstance(document['surface'], str) or not document['surface']
            or not isinstance(document['chunk_key'], str) or canonical(document) != encoded
            or surface is not None and document['surface'] != surface
            or chunk_key is not None and document['chunk_key'] != chunk_key):
        raise ValueError('Unsupported prepared output document or owner')
    return document


def _read(filename, digest, size, *, surface=None, chunk_key=None):
    if type(size) is not int or not 0 < size <= MAX_BYTES:
        raise ValueError('Invalid prepared output byte range')
    path = Path(filename)
    with fao.open_reader(path) as stream:
        encoded = stream.read(size + 1)
    if path.stem != digest or len(encoded) != size or hashlib.sha256(encoded).hexdigest() != digest:
        raise ValueError('Prepared output does not match its digest')
    _decode(encoded, surface, chunk_key)
    return encoded


def read(filename, digest, size, *, surface, chunk_key):
    return _decode(_read(filename, digest, size, surface=surface, chunk_key=chunk_key))['output']


def references(conn):
    for owner in OUTPUT_OWNERS:
        if conn.execute('SELECT 1 FROM sqlite_schema WHERE type=\'table\' AND name=?', (owner,)).fetchone():
            fields = "'album_notes',browse_id" if owner == 'album_note_outputs' else 'surface,chunk_key'
            for row in conn.execute(f'SELECT id,{fields},filename,content_digest,byte_size FROM {owner} ORDER BY id'):
                yield owner, *row


def _manifest(root, value):
    encoded = canonical({'format': BACKUP_FORMAT, **value})
    if len(encoded) > MANIFEST_BYTES:
        raise ValueError('Prepared output backup manifest exceeds its byte budget')
    return policy_documents._publish_encoded(root, encoded, hashlib.sha256(encoded).hexdigest())


def _load_manifest(path):
    path = Path(path)
    with fao.open_reader(path) as stream:
        encoded = stream.read(MANIFEST_BYTES + 1)
    if len(encoded) > MANIFEST_BYTES or hashlib.sha256(encoded).hexdigest() != path.stem:
        raise ValueError('Prepared output manifest does not match its digest')
    value = json.loads(encoded)
    if not isinstance(value, dict) or value.get('format') != BACKUP_FORMAT or canonical(value) != encoded:
        raise ValueError('Unsupported prepared output backup')
    return value


def export(conn, root):
    return _export(conn, root, directory=directory, references=references)


def _export(conn, root, *, directory, references):
    if not conn.in_transaction:
        raise ValueError('Prepared output export requires a pinned database snapshot')
    root = Path(root)
    if root.resolve() == directory(conn).resolve():
        raise ValueError('Prepared output backup requires a separate directory')
    entries, chunks = [], []
    for owner, identity, surface, key, filename, digest, size in references(conn):
        check_cancellable_work()
        encoded = _read(filename, digest, size, surface=surface, chunk_key=key)
        copied, actual, actual_size = publish(root, encoded)
        if (actual, actual_size) != (digest, size):
            raise ValueError('Prepared output backup changed its content')
        entries.append({'owner': owner, 'id': identity, 'surface': surface, 'chunk_key': key, 'digest': digest, 'byte_size': size,
                        'relative_filename': str(Path(copied).relative_to(root))})
        if len(entries) == 512:
            chunks.append(Path(_manifest(root, {'type': 'chunk', 'records': entries})).stem)
            entries.clear()
    if entries:
        chunks.append(Path(_manifest(root, {'type': 'chunk', 'records': entries})).stem)
    return _manifest(root, {'type': 'manifest', 'chunks': chunks})


def _digest(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def restore(conn, manifest):
    return _restore(conn, manifest, directory=directory, references=references,
                    owners=OUTPUT_OWNERS, relocate=_relocate)


def _relocate(conn, files):
    for (owner, identity), filename in files.items():
        conn.execute(f'UPDATE {owner} SET filename=? WHERE id=?', (filename, identity))


def _restore(conn, manifest, *, directory, references, owners, relocate):
    path = Path(manifest)
    root = path.parent.parent
    value = _load_manifest(path)
    if (set(value) != {'format', 'type', 'chunks'} or value['type'] != 'manifest'
            or not isinstance(value['chunks'], list) or any(not _digest(item) for item in value['chunks'])
            or len(set(value['chunks'])) != len(value['chunks'])):
        raise ValueError('Invalid prepared output backup manifest')
    entries = {}
    for digest in value['chunks']:
        chunk = _load_manifest(root / digest[:2] / (digest + '.json'))
        if (set(chunk) != {'format', 'type', 'records'} or chunk['type'] != 'chunk'
                or not isinstance(chunk['records'], list) or not 1 <= len(chunk['records']) <= 512):
            raise ValueError('Invalid prepared output backup chunk')
        for entry in chunk['records']:
            if not isinstance(entry, dict):
                raise ValueError('Invalid prepared output backup owner')
            # Earlier manifests contain only prepared_outputs, before note files
            # joined this file domain. Their owner is unambiguous.
            owner = entry.get('owner', 'prepared_outputs')
            identity = (owner, entry.get('id'))
            if (set(entry) - {'owner'} != {'id', 'surface', 'chunk_key', 'digest',
                    'byte_size', 'relative_filename'} or owner not in owners
                    or type(entry['id']) is not int or entry['id'] < 1
                    or identity in entries or not isinstance(entry['surface'], str) or not entry['surface']
                    or not isinstance(entry['chunk_key'], str) or not _digest(entry['digest'])
                    or owner == 'album_note_outputs' and entry['surface'] != 'album_notes'
                    or type(entry['byte_size']) is not int or not 0 < entry['byte_size'] <= MAX_BYTES
                    or entry['relative_filename'] != entry['digest'][:2] + '/' + entry['digest'] + '.json'):
                raise ValueError('Invalid prepared output backup owner')
            source = root / entry['relative_filename']
            # Verify every source first. A missing later chunk must never cause
            # a partial SQL relocation or acknowledge an incomplete restore.
            _read(source, entry['digest'], entry['byte_size'], surface=entry['surface'], chunk_key=entry['chunk_key'])
            entries[identity] = entry
    if conn.in_transaction:
        raise ValueError('Prepared output restore requires an idle connection')
    conn.execute('BEGIN IMMEDIATE')
    try:
        actual = {(row[0], row[1]): (row[2], row[3], row[5], row[6]) for row in references(conn)}
        expected = {key: (entry['surface'], entry['chunk_key'], entry['digest'], entry['byte_size'])
                    for key, entry in entries.items()}
        if actual != expected:
            raise ValueError('Prepared output backup does not match database owners')
        files = {}
        for key, entry in entries.items():
            encoded = _read(root / entry['relative_filename'], entry['digest'], entry['byte_size'],
                            surface=entry['surface'], chunk_key=entry['chunk_key'])
            files[key] = publish(directory(conn), encoded)[0]
        # All files are durable before any owner is relocated.
        relocate(conn, files)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


@dataclass(frozen=True)
class CollectionProgress:
    checked: int = 0
    files: int = 0
    byte_size: int = 0
    pending: bool = False
    blocked: bool = False


class OrphanCollector:
    """Keep a finite file batch across retries, without loading all SQL owners.

    The writer reservation covers current owners and in-flight file publishers;
    the exclusive lifetime lock covers older readers and concurrent backups.
    Restarting a sweep is safe: changed directories are synced before releasing
    protection, including when a batch is interrupted.
    """
    def __init__(self, root, *, owners=None):
        self.root = Path(root)
        self.owners = tuple((owner, 'filename') for owner in OUTPUT_OWNERS) if owners is None else tuple(owners)
        self.paths = (path for path in self.root.glob('*/*.json'))
        self.batch = None
        self.removed = self.byte_size = 0

    def close(self):
        self.paths.close()
        self.batch = None

    def step(self, conn, *, limit=128):
        if conn.in_transaction:
            raise ValueError('Prepared output collection requires an idle connection')
        if not 1 <= limit <= 128:
            raise ValueError('Invalid prepared output collection batch')
        if self.batch is None:
            self.batch = list(islice(self.paths, limit))
        if not self.batch:
            self.close()
            return CollectionProgress()
        try:
            conn.execute('BEGIN IMMEDIATE')
            with (self.root / '.readers.lock').open('a+b') as stream:
                unlock = policy_documents._try_file_lock(stream, exclusive=True)
                if unlock is None:
                    return CollectionProgress(pending=True, blocked=True)
                try:
                    names = [str(path) for path in self.batch]
                    marks = ','.join('?' for _ in names)
                    owned = set()
                    for owner, column in self.owners:
                        check_cancellable_work()
                        if conn.execute("SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name=?", (owner,)).fetchone():
                            owned.update(row[0] for row in conn.execute(
                                f'SELECT DISTINCT {column} FROM {owner} WHERE {column} IN ({marks})', names))
                    changed = set()
                    try:
                        for path in self.batch:
                            check_cancellable_work()
                            if str(path) not in owned:
                                try:
                                    length = path.stat().st_size
                                    path.unlink()
                                except FileNotFoundError:
                                    continue
                                changed.add(path.parent)
                                self.removed += 1
                                self.byte_size += length
                    finally:
                        for parent in changed:
                            _sync_directory(parent)
                    checked = len(self.batch)
                    self.batch = None
                    pending = checked == limit
                    if not pending:
                        self.close()
                    progress = CollectionProgress(checked, self.removed, self.byte_size, pending)
                    self.removed = self.byte_size = 0
                    return progress
                finally:
                    unlock()
        finally:
            conn.rollback()


def collect_orphans(conn):
    collector = OrphanCollector(directory(conn))
    removed = 0
    try:
        while True:
            progress = collector.step(conn)
            removed += progress.files
            if not progress.pending or progress.blocked:
                return removed
    finally:
        collector.close()
