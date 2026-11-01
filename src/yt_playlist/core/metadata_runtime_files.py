"""PK-addressed metadata documents. SQL stores no paths or content checksums.

Revisions are immutable: write the file before publishing its revision in SQL.
A rolled-back publication leaves an unreferenced file, never a changed old head.
"""
import hashlib
import json
from pathlib import Path
import tempfile

from .evidence_ledger import canonical, _sync_directory
from .activity import check_cancellable_work, WorkBudgetExceeded
from . import fao

MAX_BYTES = 8 * 1024 * 1024
KINDS = frozenset(('alternatives', 'native', 'choices', 'conflicts'))


def root_for_database(path, memory_key):
    if path:
        path = Path(path).resolve()
        return path.with_name(path.name + '.metadata-runtime')
    return Path(tempfile.gettempdir()) / 'tuneconsole-metadata-runtime' / str(memory_key)


def directory(conn):
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    return root_for_database(path, getattr(conn, '_policy_memory_key', id(conn)))


def relative(kind, owner, revision):
    if kind not in KINDS or not owner:
        raise ValueError('Unknown runtime metadata file owner')
    parts = []
    for key in owner:
        if type(key) is int and key > 0:
            parts.append(str(key))
        elif kind == 'native' and isinstance(key, str) and key:
            # The source's existing compound primary key can exceed a filesystem
            # component limit. Its deterministic encoding is never stored in SQL.
            parts.append(hashlib.sha256(key.encode()).hexdigest())
        else:
            raise ValueError('Invalid runtime metadata primary key')
    if type(revision) is int and revision >= 0:
        name = str(revision)
    elif type(revision) is float and revision == revision and abs(revision) < float('inf'):
        name = revision.hex()
    else:
        raise ValueError('Invalid runtime metadata revision')
    return Path(kind, parts[0][-2:].zfill(2), *parts, name + '.json')


def publish(conn, kind, owner, revision, data):
    if not conn.in_transaction:
        raise RuntimeError('Runtime metadata publication requires a writer transaction')
    body = {'format': 1, 'kind': kind, 'owner': list(owner), 'revision': revision, 'data': data}
    encoded = canonical({'body': body, 'sha256': hashlib.sha256(canonical(body)).hexdigest()})
    if len(encoded) > MAX_BYTES:
        raise ValueError('Runtime metadata document exceeds its byte budget')
    path = directory(conn) / relative(kind, owner, revision)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        old = fao.read_bytes(path, max_bytes=MAX_BYTES)
    except FileNotFoundError:
        old = None
    if old is not None:
        if old == encoded:
            return path
        # A failed transaction may leave its next revision on disk. Only a
        # committed/current revision is immutable; no snapshot owns an orphan.
        from . import metadata_runtime as runtime
        current = None
        if runtime.active(conn):
            if kind in ('choices', 'conflicts'):
                current = conn.execute('SELECT revision FROM metadata_runtime_'+kind+' WHERE id=?', owner).fetchone()
            elif kind == 'native':
                current = conn.execute('SELECT revision FROM rec_musical_sources WHERE source_key=?', owner).fetchone()
            else:
                current = conn.execute('SELECT s.at FROM enrichment_provider_state s JOIN metadata_runtime_providers p '
                    'ON p.name=s.provider WHERE s.recording_id=? AND p.id=?', owner).fetchone()
        if current is not None and revision <= current[0]:
            raise ValueError('Runtime metadata revision cannot change its content')
    # The owner's SQL reservation fences this revision's publication.
    fao.write_bytes(path, encoded, max_bytes=MAX_BYTES)
    return path


def read(conn, kind, owner, revision, *, missing=False, max_bytes=MAX_BYTES):
    path = directory(conn) / relative(kind, owner, revision)
    try:
        with fao.open_reader(path) as stream:
            encoded = stream.read(min(max_bytes, MAX_BYTES) + 1)
    except FileNotFoundError:
        if missing:
            return None
        raise ValueError('Missing runtime metadata document: ' + str(path)) from None
    if len(encoded) > min(max_bytes, MAX_BYTES):
        raise WorkBudgetExceeded('Runtime metadata document exceeds its byte budget')
    document = json.loads(encoded)
    body = document.get('body') if isinstance(document, dict) else None
    if (not isinstance(body, dict) or set(document) != {'body', 'sha256'}
            or body.get('format') != 1 or body.get('kind') != kind
            or body.get('owner') != list(owner) or body.get('revision') != revision
            or hashlib.sha256(canonical(body)).hexdigest() != document['sha256']):
        raise ValueError('Runtime metadata document differs from its owner or checksum')
    return body['data']


def references(conn, *, include_expired=False):
    """Current serving/model facts and retained manual decisions, under a snapshot."""
    from . import metadata_runtime as runtime
    if not runtime.active(conn):
        return
    import time
    cutoff = float('-inf') if include_expired else time.time()-runtime.ALTERNATIVE_SECONDS
    for recording, code, at in conn.execute('SELECT s.recording_id,p.id,s.at FROM enrichment_provider_state s '
        'JOIN metadata_runtime_providers p ON p.name=s.provider WHERE s.at>?', (cutoff,)):
        yield 'alternatives', (recording, code), float(at), False
    for key, revision in conn.execute('SELECT source_key,revision FROM rec_musical_sources'):
        yield 'native', (key,), revision, True
    for identity, revision in conn.execute('SELECT id,revision FROM metadata_runtime_choices'):
        for number in range(1, revision + 1):
            yield 'choices', (identity,), number, True
    for identity, revision in conn.execute('SELECT id,revision FROM metadata_runtime_conflicts'):
        yield 'conflicts', (identity,), revision, True


def export(conn, root):
    """Return a manifest outside SQL; deterministic paths survive a data-root move."""
    import shutil
    if not conn.in_transaction:
        raise ValueError('Runtime metadata export requires a pinned database snapshot')
    root = Path(root)
    entries = []
    for kind, owner, revision, required in references(conn):
        check_cancellable_work()
        value = read(conn, kind, owner, revision, missing=not required)
        if value is None and not required:
            continue
        relative_path = relative(kind, owner, revision)
        source, target = directory(conn) / relative_path, root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        fao.copy(source, target, max_bytes=MAX_BYTES)
        entries.append({'kind': kind, 'owner': list(owner), 'revision': revision,
                        'bytes': fao.stat(target).logical_bytes, 'sha256': hashlib.sha256(fao.read_bytes(target)).hexdigest()})
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / 'manifest.json'
    fao.write_bytes(manifest, canonical({'format': 1, 'entries': entries}))
    return str(manifest)


def restore(conn, manifest):
    """Copy owned files, checking custody; no SQL locator needs rewriting."""
    import shutil
    manifest = Path(manifest)
    document = json.loads(fao.read_bytes(manifest))
    if not isinstance(document, dict) or document.get('format') != 1 or not isinstance(document.get('entries'), list):
        raise ValueError('Invalid runtime metadata backup')
    expected = {(kind, tuple(owner), revision): required for kind, owner, revision, required in references(conn, include_expired=True)}
    seen = set()
    for entry in document['entries']:
        check_cancellable_work()
        key = entry['kind'], tuple(entry['owner']), entry['revision']
        if key not in expected or key in seen:
            raise ValueError('Runtime metadata backup differs from SQL ownership')
        seen.add(key)
        relative_path = relative(*key)
        source = manifest.parent / relative_path
        with fao.open_reader(source) as stream:
            encoded = stream.read(MAX_BYTES+1)
        if len(encoded) > MAX_BYTES:
            raise ValueError('Runtime metadata backup document exceeds its byte budget')
        if len(encoded) != entry['bytes'] or hashlib.sha256(encoded).hexdigest() != entry['sha256']:
            raise ValueError('Runtime metadata backup file failed verification')
        target = directory(conn) / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        fao.copy(source, target, max_bytes=MAX_BYTES)
        read(conn, *key)
    if any(required and key not in seen for key, required in expected.items()):
        raise ValueError('Runtime metadata backup is missing a required document')
