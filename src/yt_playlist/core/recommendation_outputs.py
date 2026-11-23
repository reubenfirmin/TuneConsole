"""Bounded opaque output files owned directly by recommendation publications."""
import hashlib
import json
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
import tempfile
import time
from contextlib import contextmanager

from . import policy_documents, publication_files, fao, file_collection, recommendation_chunks
from .activity import check_cancellable_work
from . import worker_settings
from .evidence_ledger import canonical, _sync_directory
from .recommendation_content import (
    FILE_IDENTITY, FILE_KIND, FORMAT, MAX_BYTES, decode as _decode, decode_json, encode, read,
    read_encoded as _read, record_id, record_path,
)

BACKUP_FORMAT = 'prepared-recommendation-backup-1'
MANIFEST_BYTES = 8 * 1024 * 1024
REPORT_OWNER = 'recommendation_report_files'
REPORT_PARTS = 'recommendation_report_parts'
AUTOTUNE_OWNER = 'model_autotune_output'
AUTOTUNE_SURFACE = 'rec_autotune_result'
AUTOTUNE_SETTING = 'rec_autotune_reference'
RADIO_SEED_OWNER = 'radio_seed_output'
RADIO_SEED_SURFACE = 'radio_seed_offer'
RADIO_SEED_SETTING = 'radio_seed_reference'
SAVED_OUTPUTS = {
    RADIO_SEED_OWNER: (RADIO_SEED_SETTING, RADIO_SEED_SURFACE),
    AUTOTUNE_OWNER: (AUTOTUNE_SETTING, AUTOTUNE_SURFACE),
}
OUTPUT_OWNERS = (recommendation_chunks.OWNER, 'listening_output_files', 'home_display_files',
                 REPORT_OWNER, *SAVED_OUTPUTS)
PUBLICATION_TABLES = {**publication_files.OWNERS, REPORT_OWNER: publication_files.REPORT_TABLE}


def saved_reference(conn, owner):
    raw = worker_settings.read(conn, SAVED_OUTPUTS[owner][0])
    return json.loads(raw) if raw is not None else None


def autotune_reference(conn):
    return saved_reference(conn, AUTOTUNE_OWNER)


def _report_relation(conn):
    # File export and relocation can open the source of an upgrade offline.
    exists = conn.execute('SELECT 1 FROM sqlite_schema WHERE name=?', (REPORT_PARTS,)).fetchone()
    return REPORT_PARTS if exists else REPORT_OWNER


def _has_relation(conn, relation):
    return conn.execute('SELECT 1 FROM sqlite_schema WHERE name=?', (relation,)).fetchone() is not None


def _relation(conn, owner):
    if owner == REPORT_OWNER:
        return _report_relation(conn)
    if owner == recommendation_chunks.OWNER and _has_relation(conn, recommendation_chunks.VIEW):
        return recommendation_chunks.VIEW
    return owner


def _has_manifest(conn, table):
    return not publication_files.installed_table(conn, table) or conn.execute(
        "SELECT 1 FROM pragma_table_info(?) WHERE name='files'", (table,)).fetchone() is not None


def root_for_database(path, memory_key):
    if path:
        database = Path(path).resolve()
        return database.with_name(database.name + '.recommendations')
    return Path(tempfile.gettempdir()) / 'tuneconsole-recommendations' / memory_key


def directory(conn):
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    return root_for_database(path, getattr(conn, '_policy_memory_key', str(id(conn))))


def publish(root, encoded, *, age=None):
    if not isinstance(encoded, bytes) or len(encoded) > MAX_BYTES:
        raise ValueError('Invalid prepared output bytes')
    digest = hashlib.sha256(encoded).hexdigest()
    # Validate the exact bytes before exposing a durable path to an SQL owner.
    _decode(encoded)
    return policy_documents._publish_encoded(root, encoded, digest, age=age), digest, len(encoded)


def references(conn):
    for owner in OUTPUT_OWNERS:
        relation = _relation(conn, owner)
        if _has_relation(conn, relation):
            for row in conn.execute(
                    f'SELECT id,surface,chunk_key,filename,content_digest,byte_size FROM {relation} ORDER BY id'):
                yield owner, *row
        elif owner in SAVED_OUTPUTS and _has_settings(conn):
            reference = saved_reference(conn, owner)
            if reference is not None:
                yield owner, 1, SAVED_OUTPUTS[owner][1], '', reference['filename'], reference['content_digest'], reference['byte_size']
        elif owner in PUBLICATION_TABLES and _has_settings(conn):
            table = PUBLICATION_TABLES[owner]
            if _has_manifest(conn, table):
                for row in publication_files.references(conn, table):
                    yield owner, *row


def _has_settings(conn):
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='settings'").fetchone() is not None


def _manifest(root, value):
    encoded = canonical({'format': BACKUP_FORMAT, **value})
    if len(encoded) > MANIFEST_BYTES:
        raise ValueError('Prepared output backup manifest exceeds its byte budget')
    return policy_documents._publish_encoded(root, encoded, hashlib.sha256(encoded).hexdigest())


def _load_manifest(path):
    path = Path(path)
    with fao.read(path) as stream:
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
        _read(filename, digest, size, surface=surface, chunk_key=key)
        copied = root / digest[:2] / (digest + '.json')
        # Preserve the existing encoding after domain validation. FAO checks
        # the owner's digest again before replacing any earlier backup.
        fao.write(copied, fao.read(filename, max_bytes=MAX_BYTES, digest=digest))
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
    manifests = {}
    for (owner, identity), filename in files.items():
        relation = _relation(conn, owner)
        if owner in (*PUBLICATION_TABLES, *SAVED_OUTPUTS) and not _has_relation(conn, relation):
            if owner in SAVED_OUTPUTS:
                conn.execute("UPDATE settings SET value=json_set(value,'$.filename',?) WHERE key=?",
                             (filename, SAVED_OUTPUTS[owner][0]))
            else:
                table = PUBLICATION_TABLES[owner]
                manifests.setdefault(table, {})[identity] = filename
        elif relation == recommendation_chunks.VIEW:
            recommendation_chunks.relocate(conn, identity, filename)
        else:
            conn.execute(f'UPDATE {relation} SET filename=? WHERE id=?', (filename, identity))
    with policy_documents.coordinate_updates(conn, manifests):
        for table, replacements in manifests.items():
            publication_files.relocate(conn, table, replacements)


def _file_receipt(conn, identity):
    # Offline recovery also reads databases before migration has run.
    if conn.execute("SELECT 1 FROM sqlite_schema WHERE name='recommendation_file_writes'").fetchone():
        return conn.execute('SELECT output_owner,surface,chunk_key,content_digest,byte_size,file_status '
                            'FROM recommendation_file_writes WHERE id=?', (identity,)).fetchone()
    return conn.execute('SELECT owner_id->>1,owner_id->>2,owner_id->>3,content_digest,byte_size,file_status '
                        f"FROM collection_file_writes WHERE kind='{FILE_KIND}' AND {FILE_IDENTITY}=?",
                        (identity,)).fetchone()


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
            owner = entry.get('owner', recommendation_chunks.OWNER)
            identity = (owner, entry.get('id'))
            if (set(entry) - {'owner'} != {'id', 'surface', 'chunk_key', 'digest',
                    'byte_size', 'relative_filename'} or owner not in owners
                    or type(entry['id']) is not int or entry['id'] < 1
                    or identity in entries or not isinstance(entry['surface'], str) or not entry['surface']
                    or not isinstance(entry['chunk_key'], str) or not _digest(entry['digest'])
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
        retained = list(references(conn))
        actual = {(row[0], row[1]): (row[2], row[3], row[5], row[6]) for row in retained}
        expected = {key: (entry['surface'], entry['chunk_key'], entry['digest'], entry['byte_size'])
                    for key, entry in entries.items()}
        if actual != expected:
            raise ValueError('Prepared output backup does not match database owners')
        identities = {}
        for owner, identity, surface, key, filename, digest, size in retained:
            file_id = record_id(filename)
            if file_id is not None:
                row = _file_receipt(conn, file_id)
                if row is None or tuple(row) != (owner, surface, key, digest, size, 'ready'):
                    raise ValueError('Prepared output backup has an inconsistent file identity')
                identities[owner, identity] = file_id
        files = {}
        from .recommendation_files import RecommendationFiles
        destination = RecommendationFiles(directory(conn))
        for key, entry in entries.items():
            encoded, age = _read(root / entry['relative_filename'], entry['digest'], entry['byte_size'],
                                surface=entry['surface'], chunk_key=entry['chunk_key'], with_age=True)
            if key in identities:
                files[key] = destination.publish_reserved(identities[key], encoded,
                    expected=(entry['digest'], entry['byte_size']), age=age)[0]
            else:
                files[key] = publish(destination.root, encoded, age=age)[0]
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
        self.paths = file_collection.walk_entries(self.root, max_depth=2)
        self.batch = None
        self._batch_entries = 0
        self.removed = self.byte_size = 0
        self._collecting = False
        self._batch_limit = 128

    def close(self):
        self.paths.close()
        self.batch = None
        self._batch_entries = 0

    @contextmanager
    def candidates(self, *, limit=128):
        if not 1 <= limit <= 128:
            raise ValueError('Invalid prepared output collection batch')
        self._batch_limit = limit
        if self.batch is None:
            entries = list(islice(self.paths, limit))
            self._batch_entries = len(entries)
            self.batch = [path for path in entries if path is not None and path.suffix == '.json']
        if not self._batch_entries:
            yield []
            return
        with (self.root / '.readers.lock').open('a+b') as stream:
            unlock = policy_documents._try_file_lock(stream, exclusive=True)
            if unlock is None:
                yield None
                return
            try:
                self._collecting = True
                yield [str(path) for path in self.batch]
            finally:
                self._collecting = False
                unlock()

    def remove_unowned(self, owned):
        if not self._batch_entries:
            self.close()
            return CollectionProgress()
        if not self._collecting:
            raise RuntimeError('Prepared output collection requires its file protection')
        changed = set()
        try:
            for path in self.batch:
                check_cancellable_work()
                if str(path) not in owned:
                    try:
                        info = fao.read(path).info
                        if record_id(path) is not None and info.ctime_ns / 1e9 > time.time() - 30 * 86400:
                            continue
                        length = info.compressed_bytes
                        if not fao.delete(path, missing_ok=True):
                            continue
                    except FileNotFoundError:
                        continue
                    changed.add(path.parent)
                    self.removed += 1
                    self.byte_size += length
        finally:
            for parent in changed:
                _sync_directory(parent)
        checked = self._batch_entries
        self.batch = None
        self._batch_entries = 0
        pending = checked == self._batch_limit
        if not pending:
            self.close()
        progress = CollectionProgress(checked, self.removed, self.byte_size, pending)
        self.removed = self.byte_size = 0
        return progress

    def step(self, conn, *, limit=128):
        """Installed-format compatibility; runtime controllers supply SQL facts separately."""
        if conn.in_transaction:
            raise ValueError('Prepared output collection requires an idle connection')
        try:
            conn.execute('BEGIN IMMEDIATE')
            with self.candidates(limit=limit) as names:
                if names is None:
                    return CollectionProgress(pending=True, blocked=True)
                owned = set()
                if names:
                    names = [str(path) for path in self.batch]
                    marks = ','.join('?' for _ in names)
                    for owner, column in self.owners:
                        check_cancellable_work()
                        relation = _relation(conn, owner)
                        if _has_relation(conn, relation):
                            owned.update(row[0] for row in conn.execute(
                                f'SELECT DISTINCT {column} FROM {relation} WHERE {column} IN ({marks})', names))
                        elif owner in SAVED_OUTPUTS and _has_settings(conn):
                            owned.update(protected_saved(conn, owner, names))
                        elif owner in PUBLICATION_TABLES and _has_settings(conn):
                            table = PUBLICATION_TABLES[owner]
                            if _has_manifest(conn, table):
                                owned.update(publication_files.protected(conn, table, names))
                return self.remove_unowned(owned)
        finally:
            conn.rollback()


def protected_saved(conn, owner, names):
    reference = saved_reference(conn, owner)
    return {reference['filename']} if reference and reference['filename'] in names else set()


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
