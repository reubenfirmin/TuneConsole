"""Metadata file ports and installed-format preparation adapters.

RuntimeMetadataFiles receives detached SQL receipt IDs and verifies their bytes.
Connection-based helpers retain the installed revision layout for explicit
preparation and recovery; normal runtime publication uses the injected file port.
"""
import hashlib
import json
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from contextlib import contextmanager, ExitStack
from pathlib import Path
import tempfile

from .evidence_ledger import canonical
from .activity import check_cancellable_work, WorkBudgetExceeded
from . import fao, policy_documents, runtime_metadata_choices as choices
from .metadata_contract import ALTERNATIVE_SECONDS, runtime_active
from . import metadata_file_contract as contract

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


def _prepared(conn, kind, owner, revision, data):
    if not conn.in_transaction:
        raise RuntimeError('Runtime metadata publication requires a writer transaction')
    current = None
    if runtime_active(conn):
        if kind == 'choices':
            current = conn.execute(f'SELECT revision FROM {choices.VIEW} WHERE id=?', owner).fetchone()
        elif kind == 'conflicts':
            current = conn.execute('SELECT runtime_revision FROM metadata_conflicts WHERE runtime_id=?', owner).fetchone()
        elif kind == 'native':
            current = conn.execute('SELECT revision FROM rec_musical_sources WHERE source_key=?', owner).fetchone()
        elif kind == 'alternatives':
            current = conn.execute('SELECT s.at FROM enrichment_provider_state s JOIN enrichment_provider_config p '
                "ON p.provider=s.provider WHERE s.recording_id=? AND p.metadata_provider_id=? AND s.status<>'queued'", owner).fetchone()
    return _prepared_at(directory(conn), kind, owner, revision, data,
                        current_revision=current[0] if current is not None else None)


def _prepared_at(root, kind, owner, revision, data, *, current_revision=None):
    body = {'format': 1, 'kind': kind, 'owner': list(owner), 'revision': revision, 'data': data}
    encoded = canonical({'body': body, 'sha256': hashlib.sha256(canonical(body)).hexdigest()})
    if len(encoded) > MAX_BYTES:
        raise ValueError('Runtime metadata document exceeds its byte budget')
    path = Path(root) / relative(kind, owner, revision)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        old = fao.read(path, max_bytes=MAX_BYTES).bytes
    except FileNotFoundError:
        old = None
    if old is not None:
        if old == encoded:
            # An identical orphan can be reused after a rolled-back SQL write.
            return path, None
        # A failed transaction may leave its next revision on disk. Only a
        # committed/current revision is immutable; no snapshot owns an orphan.
        if current_revision is not None and revision <= current_revision:
            raise ValueError('Runtime metadata revision cannot change its content')
    return path, encoded


def publish(conn, kind, owner, revision, data):
    path, encoded = _prepared(conn, kind, owner, revision, data)
    # The owner's SQL reservation fences this revision's publication.
    if encoded is not None:
        fao.write(path, encoded)
    return path


class RuntimeMetadataFiles:
    """Read and publish metadata selected by explicit SQL identities and revisions."""

    def __init__(self, root):
        self.root = Path(root)
        self._alternatives_gc = None

    def retained(self):
        return policy_documents.retained_reader(self.root)

    describe = staticmethod(contract.describe)

    @contextmanager
    def write_reserved(self, records):
        with ExitStack() as retained:
            for record in records:
                encoded = contract.encode(record['kind'], record['owner'], record['revision'], record['data'])
                if len(encoded) != record['size'] or hashlib.sha256(encoded).hexdigest() != record['digest']:
                    raise ValueError('Runtime metadata document changed after reservation')
                pending = retained.enter_context(fao.prepare(contract.record_path(self.root, record['file_id']),
                    fao.Content(encoded, max_bytes=MAX_BYTES)))
                pending.publish()
            yield

    def publish(self, kind, owner, revision, data, *, current_revision=None):
        path, encoded = _prepared_at(self.root, kind, owner, revision, data,
                                     current_revision=current_revision)
        if encoded is not None:
            fao.write(path, encoded)
        return path

    def read(self, kind, owner, revision, *, missing=False, reference=None):
        return read_at(self.root, kind, owner, revision, missing=missing, reference=reference)

    def collecting(self):
        from .file_collection import collector
        return collector(self.root)

    def delete_reserved(self, identities):
        return sum(fao.delete(contract.record_path(self.root, identity), missing_ok=True) for identity in identities)

    def close(self):
        if self._alternatives_gc is not None:
            self._alternatives_gc[1].close()
            self._alternatives_gc = None

    def expire_alternatives(self, shard, now, *, budget=128):
        """Charge directory entries and shard transitions against the same budget."""
        removed = 0
        for _ in range(budget):
            if self._alternatives_gc is None or self._alternatives_gc[0] != shard:
                self.close()
                self._alternatives_gc = shard, _walk_alternatives(self.root / 'alternatives' / str(shard).zfill(2))
            try:
                entry, depth = next(self._alternatives_gc[1])
            except StopIteration:
                self.close()
                shard = (shard + 1) % 100
                continue
            if depth != 2 or not entry.name.endswith('.json') or not entry.is_file(follow_symlinks=False):
                continue
            try:
                at = float.fromhex(entry.name[:-5])
            except ValueError:
                continue
            if at <= now - ALTERNATIVE_SECONDS:
                removed += fao.delete(entry.path, missing_ok=True)
        return removed, shard

    def prune_revisions(self, kind, owner, keep, *, budget=128):
        parent = (self.root / relative(kind, owner, keep[0])).parent
        removed = 0
        if not parent.exists():
            return 0
        for index, path in enumerate(parent.iterdir()):
            if index == budget:
                break
            if path.suffix == '.json' and path.stem.isdecimal() and int(path.stem) not in keep:
                removed += fao.delete(path, missing_ok=True)
        return removed


def _walk_alternatives(root, depth=0):
    import os
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                yield entry, depth
                if depth < 2 and entry.is_dir(follow_symlinks=False):
                    yield from _walk_alternatives(entry.path, depth + 1)
    except FileNotFoundError:
        return


def publish_many(conn, documents):
    """Durably write at most 128 documents before the caller commits SQL.

    Owner checks and encoding stay on the SQLite thread. Four file writers
    overlap durable I/O, holding at most 8 MiB of pending encoded documents.
    Every writer has stopped before this call returns or raises.
    """
    if not conn.in_transaction:
        raise RuntimeError('Runtime metadata publication requires a writer transaction')

    pending, size, seen = deque(), 0, set()
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix='metadata-files') as pool:
        for count, document in enumerate(documents, 1):
            check_cancellable_work()
            if count > 128:
                raise ValueError('Runtime metadata batch exceeds its document budget')
            if len(pending) == 4:
                future, length = pending.popleft()
                future.result()
                size -= length
            path, encoded = _prepared(conn, *document)
            if path in seen:
                raise ValueError('Runtime metadata batch repeats a revision')
            seen.add(path)
            if encoded is None:
                continue
            while pending and size + len(encoded) > MAX_BYTES:
                future, length = pending.popleft()
                future.result()
                size -= length
            future = pool.submit(copy_context().run, fao.write, path, encoded)
            pending.append((future, len(encoded)))
            size += len(encoded)
        for future, _ in pending:
            future.result()


def read(conn, kind, owner, revision, *, missing=False, max_bytes=MAX_BYTES):
    from yt_playlist.repos import metadata_file_writes
    return read_at(directory(conn), kind, owner, revision, missing=missing, max_bytes=max_bytes,
                   reference=metadata_file_writes.reference(conn, kind, owner, revision))


def reference_path(conn, kind, owner, revision):
    from yt_playlist.repos import metadata_file_writes
    reference = metadata_file_writes.reference(conn, kind, owner, revision)
    root = directory(conn)
    return contract.record_path(root, reference[0]) if reference else root / relative(kind, owner, revision)


def read_at(root, kind, owner, revision, *, missing=False, max_bytes=MAX_BYTES, reference=None):
    """Read a SQL-selected document using an injected directory and primary keys."""
    path = contract.record_path(root, reference[0]) if reference is not None else Path(root) / relative(kind, owner, revision)
    if reference is not None and (type(reference[2]) is not int or not 0 < reference[2] <= min(max_bytes, MAX_BYTES)):
        raise WorkBudgetExceeded('Runtime metadata document exceeds its byte budget')
    try:
        with fao.read(path) as stream:
            encoded = stream.read(min(max_bytes, MAX_BYTES) + 1)
    except FileNotFoundError:
        if missing:
            return None
        raise ValueError('Missing runtime metadata document: ' + str(path)) from None
    if len(encoded) > min(max_bytes, MAX_BYTES):
        raise WorkBudgetExceeded('Runtime metadata document exceeds its byte budget')
    if reference is not None and (len(encoded) != reference[2] or hashlib.sha256(encoded).hexdigest() != reference[1]):
        raise ValueError('Runtime metadata file differs from its SQL receipt')
    return _verified_data(encoded, kind, owner, revision)


def _verified_data(encoded, kind, owner, revision):
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
    if runtime_active(conn):
        yield from active_references(conn, include_expired=include_expired)


def active_references(conn, *, include_expired=False, choice_relation=choices.VIEW):
    """List references for a snapshot whose metadata serving state is active."""
    import time
    cutoff = float('-inf') if include_expired else time.time()-ALTERNATIVE_SECONDS
    for recording, code, at in conn.execute('SELECT s.recording_id,p.metadata_provider_id,s.at FROM enrichment_provider_state s '
        'JOIN enrichment_provider_config p ON p.provider=s.provider AND p.metadata_provider_id IS NOT NULL '
        "WHERE s.at>? AND s.status<>'queued'", (cutoff,)):
        yield 'alternatives', (recording, code), float(at), False
    for key, revision in conn.execute('SELECT source_key,revision FROM rec_musical_sources'):
        yield 'native', (key,), revision, True
    for identity, revision in conn.execute(f'SELECT id,revision FROM {choice_relation}'):
        for number in range(1, revision + 1):
            yield 'choices', (identity,), number, True
    for identity, revision in conn.execute('SELECT runtime_id,runtime_revision FROM metadata_conflicts WHERE runtime_id IS NOT NULL'):
        yield 'conflicts', (identity,), revision, True


def owns_file(conn, filename):
    """Keep committed heads/history; collect valid ownerless documents after grace.

    Caller holds the domain snapshot fence and a SQL writer reservation.
    Inspect one bounded document because native source keys are hashed in paths.
    Unknown or damaged evidence is retained rather than guessed to be orphaned.
    Prepared inactive heads have the same ownership rules as active heads.
    """
    identity = contract.record_id(filename)
    if identity is not None:
        from yt_playlist.repos import metadata_file_writes
        return metadata_file_writes.retains(conn, identity)
    try:
        encoded = fao.read(filename, max_bytes=MAX_BYTES).bytes
        body = json.loads(encoded)['body']
        kind, owner, revision = body['kind'], body['owner'], body['revision']
        if not isinstance(owner, list) or len(owner) != (2 if kind == 'alternatives' else 1):
            return True
        if Path(filename).resolve() != (directory(conn) / relative(kind, owner, revision)).resolve():
            return True
        _verified_data(encoded, kind, owner, revision)
    except (ValueError, KeyError, TypeError):
        return True
    if kind == 'native':
        row = conn.execute('SELECT revision FROM rec_musical_sources WHERE source_key=?', owner).fetchone()
        return row is not None and row[0] == revision
    if kind == 'choices':
        row = conn.execute(f'SELECT revision FROM {choices.VIEW} WHERE id=?', owner).fetchone()
        return row is not None and 1 <= revision <= row[0]
    if kind == 'conflicts':
        row = conn.execute('SELECT runtime_revision FROM metadata_conflicts WHERE runtime_id=?', owner).fetchone()
        return row is not None and row[0] == revision
    row = conn.execute('SELECT s.at FROM enrichment_provider_state s JOIN enrichment_provider_config p '
        "ON p.provider=s.provider WHERE s.recording_id=? AND p.metadata_provider_id=? AND s.status<>'queued'", owner).fetchone()
    return row is not None and row[0] == revision


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
        source = reference_path(conn, kind, owner, revision)
        relative_path = source.relative_to(directory(conn))
        target = root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        fao.write(target, fao.read(source, max_bytes=MAX_BYTES))
        entries.append({'kind': kind, 'owner': list(owner), 'revision': revision,
                        'bytes': fao.read(target).info.logical_bytes, 'sha256': hashlib.sha256(fao.read(target).bytes).hexdigest()})
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / 'manifest.json'
    fao.write(manifest, canonical({'format': 1, 'entries': entries}))
    return str(manifest)


def _verify_backup(conn, manifest, *, staged=None, current=False):
    """Validate membership and bytes, staging only for later publication."""
    manifest = Path(manifest)
    document = json.loads(fao.read(manifest).bytes)
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
        relative_path = reference_path(conn, *key).relative_to(directory(conn))
        prepared = manifest.parent / relative_path
        if staged is not None:
            prepared = staged / relative_path
            fao.write(prepared, fao.read(manifest.parent / relative_path, max_bytes=MAX_BYTES))
        encoded = fao.read(prepared, max_bytes=MAX_BYTES).bytes
        if len(encoded) != entry['bytes'] or hashlib.sha256(encoded).hexdigest() != entry['sha256']:
            raise ValueError('Runtime metadata backup file failed verification')
        _verified_data(encoded, *key)
        from yt_playlist.repos import metadata_file_writes
        reference = metadata_file_writes.reference(conn, *key)
        if reference and (len(encoded) != reference[2] or hashlib.sha256(encoded).hexdigest() != reference[1]):
            raise ValueError('Runtime metadata backup differs from its SQL receipt')
        if current and encoded != fao.read(directory(conn) / relative_path, max_bytes=MAX_BYTES).bytes:
            raise ValueError('Runtime metadata backup differs from current content')
    if any(required and key not in seen for key, required in expected.items()):
        raise ValueError('Runtime metadata backup is missing a required document')
    return seen


@contextmanager
def verified_backup(conn, manifest):
    """Pin private verified members before publishing any restored files."""
    with tempfile.TemporaryDirectory(prefix='tuneconsole-metadata-restore-') as staging:
        staged = Path(staging)
        yield staged, _verify_backup(conn, manifest, staged=staged)


def verify_backup(conn, manifest):
    """Establish exact independent recovery custody without rewriting live files."""
    return len(_verify_backup(conn, manifest, current=True))


def restore(conn, manifest):
    """Validate the complete backup before replacing any live owned files."""
    with verified_backup(conn, manifest) as (staged, seen):
        # Publish only the private, verified copies, so backup replacements
        # after validation cannot introduce unchecked bytes into live history.
        for key in seen:
            relative_path = reference_path(conn, *key).relative_to(directory(conn))
            fao.write(directory(conn) / relative_path, fao.read(staged / relative_path, max_bytes=MAX_BYTES))
