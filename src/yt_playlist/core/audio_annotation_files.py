"""Durable, immutable annotation documents and their explicit file lifecycle."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from contextlib import contextmanager

from yt_playlist.core.audio_annotation_contract import FILE_FORMAT
from yt_playlist.core.evidence_ledger import canonical
from .annotation_paths import record_path, record_id
from . import fao

MAX_BYTES = 4 * 1024 * 1024


def directory(conn, memory_key=None):
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    return root_for_database(path, memory_key)


def root_for_database(path, memory_key=None):
    if path:
        database = Path(path).resolve()
        return database.with_name(database.name + '.annotations')
    if memory_key is None:
        return Path(tempfile.mkdtemp(prefix='tuneconsole-annotations-'))
    return Path(tempfile.gettempdir()) / 'tuneconsole-annotations' / memory_key


class AnnotationFiles:
    def __init__(self, root):
        self.root = root

    def read(self, annotation):
        return read(annotation['model_output_filename'], expected_digest=annotation['model_output_digest'], expected={
            'run_key': annotation['run_id'] or annotation['legacy_run_key'],
            'kind': annotation['kind'], 'taxonomy_version': annotation['taxonomy_version'],
            'revision': annotation['revision']})

    @contextmanager
    def write_reserved(self, identity, document, *, expected):
        path = record_path(self.root, identity)
        encoded = _encoded(document)
        digest = hashlib.sha256(encoded).hexdigest()
        if (digest, len(encoded)) != expected:
            raise ValueError('Annotation document changed after reservation')
        with fao.prepare(path, fao.Content(encoded, max_bytes=MAX_BYTES)) as pending:
            pending.publish()
            yield str(path), digest, len(encoded)

    def describe(self, document):
        encoded = _encoded(document)
        return hashlib.sha256(encoded).hexdigest(), len(encoded)

    def retained(self):
        from .policy_documents import retained_reader
        return retained_reader(self.root)


def _encoded(document):
    if not isinstance(document, dict) or document.get('format') != FILE_FORMAT:
        raise ValueError('Unsupported annotation document format')
    encoded = canonical(document)
    if len(encoded) > MAX_BYTES:
        raise ValueError('Annotation document exceeds the input byte budget')
    return encoded


def _publication(root, document):
    encoded = _encoded(document)
    digest = hashlib.sha256(encoded).hexdigest()
    return encoded, Path(root) / digest[:2] / (digest + '.json')


def publish(root, document, *, age=None):
    """Publish before inserting its database reference, under the writer reservation."""
    encoded, path = _publication(root, document)
    fao.write(path, fao.Content(encoded, metadata=age, max_bytes=MAX_BYTES))
    return str(path)


def read(filename, *, expected=None, expected_digest=None, with_age=False):
    path = Path(filename)
    with fao.read(path) as stream:
        age = stream.info
        encoded = stream.read(MAX_BYTES + 1)
    if len(encoded) > MAX_BYTES:
        raise ValueError('Annotation document exceeds the input byte budget')
    digest = expected_digest if expected_digest is not None else path.stem
    if digest != hashlib.sha256(encoded).hexdigest():
        raise ValueError('Annotation file does not match its content hash')
    document = json.loads(encoded)
    if (not isinstance(document, dict) or document.get('format') != FILE_FORMAT
            or any(document.get(key) != value for key, value in (expected or {}).items())):
        raise ValueError('Annotation file does not match its judgment identity')
    return (document, age) if with_age else document


def export(conn, root):
    """Copy every revision's document alongside a consistent database backup.

    The caller pins the same database snapshot for this export and its database
    backup. The returned manifest is itself content addressed and durable.
    """
    if not conn.in_transaction:
        raise ValueError('Annotation export requires a pinned database snapshot')
    if Path(root).resolve() == directory(conn).resolve():
        raise ValueError('Annotation backup requires a separate destination directory')
    files = []
    # Tiny offline codec owners may have only the historical two columns.
    columns = {row[1] for row in conn.execute('PRAGMA main.table_info(audio_annotations)')}
    digest_column = 'model_output_digest' if 'model_output_digest' in columns else 'NULL'
    for annotation_id, filename, digest in conn.execute(
            f'SELECT id,model_output_filename,{digest_column} FROM audio_annotations ORDER BY id'):
        value, age = read(filename, expected_digest=digest, with_age=True)
        target = publish(root, value, age=age)
        files.append({'annotation_id': annotation_id, 'digest': Path(target).stem,
                      'relative_filename': str(Path(target).relative_to(root))})
    return publish(root, {'format': FILE_FORMAT, 'type': 'backup-manifest', 'files': files})


def restore(conn, manifest, *, memory_key=None):
    """Restore files before repointing a restored database; no judgment is rewritten."""
    document = read(manifest, expected={'type': 'backup-manifest'})
    root = Path(manifest).parent.parent
    entries = document['files']
    if len({entry['annotation_id'] for entry in entries}) != len(entries):
        raise ValueError('Duplicate annotation in backup manifest')
    if conn.in_transaction:
        raise ValueError('Annotation restore requires its own writer reservation')
    conn.execute('BEGIN IMMEDIATE')
    try:
        expected = dict(conn.execute('SELECT id,model_output_filename FROM audio_annotations'))
        if set(expected) != {entry['annotation_id'] for entry in entries}:
            raise ValueError('Annotation backup does not match the restored database')
        targets = []
        for entry in entries:
            relative = Path(entry['relative_filename'])
            digest = entry['digest']
            identity = record_id(expected[entry['annotation_id']])
            if (relative != Path(digest[:2]) / (digest + '.json')
                    or identity is None and Path(expected[entry['annotation_id']]).stem != digest):
                raise ValueError('Annotation backup identity mismatch')
            value, age = read(root / relative, with_age=True)
            if identity is not None:
                owner = conn.execute('SELECT content_digest,byte_size,annotation_id,file_status '
                    'FROM annotation_file_writes WHERE id=?', (identity,)).fetchone()
                encoded = _encoded(value)
                integrity = hashlib.sha256(encoded).hexdigest(), len(encoded)
                if owner is None or tuple(owner) != (*integrity, entry['annotation_id'], 'ready'):
                    raise ValueError('Annotation backup has an inconsistent file identity')
                target = record_path(directory(conn, memory_key), identity)
                fao.write(target, fao.Content(encoded, metadata=age, max_bytes=MAX_BYTES))
                filename = str(target)
            else:
                filename = publish(directory(conn, memory_key), value, age=age)
            targets.append((filename, entry['annotation_id']))
        conn.executemany('UPDATE audio_annotations SET model_output_filename=? WHERE id=?', targets)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def collect_orphans(conn, *, memory_key=None):
    """Remove only unreferenced files in this database's own annotation directory."""
    if conn.in_transaction:
        raise ValueError('Annotation cleanup requires its own writer reservation')
    root = directory(conn, memory_key)
    from . import file_collection
    with file_collection.collector(root) as admitted:
        if not admitted:
            return 0
        return _collect_orphans(conn, root)


def _collect_orphans(conn, root):
    from . import file_collection
    removed = 0
    conn.execute('BEGIN IMMEDIATE')
    try:
        retained = {Path(row[0]).resolve() for row in conn.execute(
            'SELECT model_output_filename FROM audio_annotations')}
        for path in file_collection.walk_entries(root, pending=True, max_depth=2):
            if path is None or path.resolve() in retained or not (path.suffix == '.json' or path.name.startswith('.fao-pending-')):
                continue
            try:
                info = fao.read(path).info
                if record_id(path) is not None and info.ctime_ns / 1e9 > time.time() - 30 * 86400:
                    continue
                removed += fao.delete(path, expected=info.identity, missing_ok=True)
            except (FileNotFoundError, fao.FileChanged):
                continue
        # Pre-FAO preparation left this different temporary-file spelling.
        for path in root.glob('*/.pending-*'):
            removed += fao.delete(path, missing_ok=True)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return removed
