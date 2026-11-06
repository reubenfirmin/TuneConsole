"""Durable, immutable annotation documents and their explicit file lifecycle."""
import hashlib
import json
import os
from pathlib import Path
import tempfile

from yt_playlist.core.audio_annotation_contract import FILE_FORMAT
from yt_playlist.core.evidence_ledger import canonical
from . import fao

MAX_BYTES = 4 * 1024 * 1024


def directory(conn, memory_key=None):
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    if path:
        database = Path(path).resolve()
        return database.with_name(database.name + '.annotations')
    if memory_key is None:
        return Path(tempfile.mkdtemp(prefix='tuneconsole-annotations-'))
    return Path(tempfile.gettempdir()) / 'tuneconsole-annotations' / memory_key


def publish(root, document, *, age=None):
    """Publish before inserting its database reference, under the writer reservation."""
    if not isinstance(document, dict) or document.get('format') != FILE_FORMAT:
        raise ValueError('Unsupported annotation document format')
    encoded = canonical(document)
    if len(encoded) > MAX_BYTES:
        raise ValueError('Annotation document exceeds the input byte budget')
    digest = hashlib.sha256(encoded).hexdigest()
    path = Path(root) / digest[:2] / (digest + '.json')
    fao.write(path, fao.Content(encoded, metadata=age, max_bytes=MAX_BYTES))
    return str(path)


def read(filename, *, expected=None, with_age=False):
    path = Path(filename)
    with fao.read(path) as stream:
        age = stream.info
        encoded = stream.read(MAX_BYTES + 1)
    if len(encoded) > MAX_BYTES:
        raise ValueError('Annotation document exceeds the input byte budget')
    if path.stem != hashlib.sha256(encoded).hexdigest():
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
    for annotation_id, filename in conn.execute(
            'SELECT id,model_output_filename FROM audio_annotations ORDER BY id'):
        value, age = read(filename, with_age=True)
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
            if relative != Path(digest[:2]) / (digest + '.json') or Path(expected[entry['annotation_id']]).stem != digest:
                raise ValueError('Annotation backup identity mismatch')
            value, age = read(root / relative, with_age=True)
            targets.append((publish(directory(conn, memory_key), value, age=age), entry['annotation_id']))
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
    removed = 0
    conn.execute('BEGIN IMMEDIATE')
    try:
        retained = {Path(row[0]).resolve() for row in conn.execute(
            'SELECT model_output_filename FROM audio_annotations')}
        if root.exists():
            for path in (*root.glob('*/*.json'), *root.glob('*/.pending-*')):
                if path.resolve() not in retained:
                    removed += fao.delete(path, missing_ok=True)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return removed
