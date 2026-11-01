"""The closed-handle database replacement and recovery boundary.

The serving lock covers successful reopening and failure recovery alike. A
request already waiting for that lock must never see a closed Store connection.
"""
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time

from .. import db_handles, policy_documents, policy_recovery
from ..evidence_ledger import _sync_directory
from . import build


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def sync(path):
    with Path(path).open('rb') as stream:
        os.fsync(stream.fileno())
    _sync_directory(Path(path).parent)


def save(database, value):
    destination = db_handles.state_path(database)
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.'+destination.name, delete=False) as stream:
        pending = Path(stream.name)
        try:
            stream.write((json.dumps(value, sort_keys=True, allow_nan=False)+'\n').encode())
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            pending.unlink(missing_ok=True)
            raise
    try:
        os.replace(pending, destination)
        _sync_directory(destination.parent)
    finally:
        pending.unlink(missing_ok=True)


def reopen(store, *, internal=False):
    # Cancellation applies to disposable build work, not connection recovery.
    from contextvars import Context
    Context().run(_open_store, store, internal)


def _open_store(store, internal):
    from ..db_diagnostics import DiagnosticConnection
    from ..db_revisions import Revisions
    from ..read_memo import ReadMemo
    factory = lambda *args, **kwargs: DiagnosticConnection(*args, _compaction_internal=internal, **kwargs)
    store.conn = sqlite3.connect(store._db_path, check_same_thread=False, factory=factory)
    try:
        store.conn._policy_memory_key = store._evidence_key
        store._configure_connection()
        store.conn.execute('PRAGMA journal_mode=WAL')
        store._bind_repositories()
        store._snapshot_revisions = Revisions(store.conn)
        store._read_memo = ReadMemo()
    except BaseException:
        store.conn.close()
        raise
    store._database_generation = getattr(store, '_database_generation', 0)+1


def _rollback(database, state):
    """Requires exclusive lifetime admission and every SQLite handle closed."""
    original = Path(state['original'])
    if state['phase'] == 'complete':
        raise ValueError('Write admission already resumed; automatic rollback is no longer safe')
    if original.exists():
        if digest(original) != state.get('original_sha256'):
            raise ValueError('Recoverable original does not match verified cutover state')
        if database.exists():
            with closing(sqlite3.connect(database, timeout=.1)) as rejected:
                checkpoint = rejected.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                if checkpoint[0] or checkpoint[1] != checkpoint[2]:
                    raise RuntimeError('Pinned SQLite handles prevent rollback')
                rejected.execute('PRAGMA journal_mode=DELETE')
                rejected.execute('BEGIN EXCLUSIVE')
                rejected.rollback()
            state['rejected_sha256'] = digest(database)
            os.replace(database, Path(state['destination'])/'rejected.db')
        os.replace(original, database)
        sync(database)
    elif not database.exists() or state['phase'] in {'original_moved', 'installed', 'reopening', 'rollback'}:
        raise ValueError('Compaction recovery cannot find its original database')
    elif state.get('original_sha256') and digest(database) != state['original_sha256']:
        raise ValueError('Compaction recovery cannot verify the original database')
    state.update(phase='failed', error='Interrupted compaction rolled back to the original database')
    save(database, state)


def replace(store, state, guard, *, progress):
    """Consume the source writer guard and install an already verified sibling.

    All rollback and reopen work stays inside this serving-lock scope, including
    failures after renaming the original and failures binding new repositories.
    """
    database = store._db_path
    closed = False
    started = time.monotonic()
    with store._lock:
        try:
            state['phase'] = 'closing'
            save(database, state)
            store.conn.close()
            closed = True
            guard.rollback()
            # Previously admitted bounded readers may finish before replacement.
            with db_handles.lease(database, exclusive=True, timeout=15):
                checkpoint = guard.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                if checkpoint[0] or checkpoint[1] != checkpoint[2]:
                    raise RuntimeError('Pinned SQLite readers prevent compaction cutover')
                if guard.execute('PRAGMA journal_mode=DELETE').fetchone()[0].lower() != 'delete':
                    raise RuntimeError('SQLite handles prevent compaction cutover')
                guard.execute('BEGIN EXCLUSIVE')
                guard.rollback()
                guard.close()
                guard = None
                state['original_sha256'] = digest(database)
                save(database, state)
                os.replace(database, Path(state['original']))
                sync(Path(state['original']))
                _sync_directory(database.parent)
                state['phase'] = 'original_moved'
                save(database, state)
                progress('original_moved')
                os.replace(Path(state['sibling']), database)
                sync(database)
                state['phase'] = 'installed'
                save(database, state)
                progress('installed')
                if digest(database) != state['candidate_sha256']:
                    raise ValueError('Installed database differs from its verified sibling')
                state['phase'] = 'reopening'
                save(database, state)
                progress('reopening')
                reopen(store, internal=True)
                state['phase'] = 'complete'
                state['phases']['installation_seconds'] = time.monotonic()-started
                save(database, state)
            store.conn._join_handles()
            closed = False
        except Exception:
            state['failed_phase'] = state['phase']
            if guard is not None:
                guard.close()
                guard = None
            if closed:
                if getattr(store.conn, '_handle_internal', False):
                    store.conn.close()
                pending = db_handles.state(database) or state
                if pending['phase'] == 'complete':
                    # Durable admission makes the new database authoritative.
                    state.update(pending)
                elif pending['phase'] == 'closing' and not Path(state['original']).exists():
                    # A reader drain timeout leaves the original path untouched.
                    state['phase'] = 'failed'
                    save(database, state)
                else:
                    with db_handles.lease(database, exclusive=True):
                        _rollback(database, state)
                reopen(store)
            raise
        finally:
            if guard is not None:
                guard.close()


def recover(database):
    """Resolve an interrupted cutover before Store opens any SQLite handle."""
    database = Path(database).resolve()
    pending = db_handles.state(database)
    if not pending or pending['phase'] not in db_handles.ACTIVE:
        return
    with owner(database), db_handles.lease(database, exclusive=True):
        pending = db_handles.state(database)
        if pending['phase'] == 'installed':
            if digest(database) != pending['candidate_sha256']:
                raise ValueError('Interrupted installed database failed its durable digest')
            with closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)) as conn:
                policy_recovery._verify(conn)
                build._format(conn)
            pending.update(phase='complete', recovered=True)
            save(database, pending)
        elif pending['phase'] in {'closing', 'original_moved', 'reopening', 'rollback'}:
            _rollback(database, pending)
        else:
            pending.update(failed_phase=pending['phase'], phase='failed',
                           error='Interrupted before installation; original database retained')
            save(database, pending)


@contextmanager
def owner(database):
    """Serialize job ownership with the same durable lock used at recovery."""
    from ..db_space import StorageCompacting
    path = database.with_name(database.name+'.compaction.lock')
    with path.open('a+b') as stream:
        unlock = policy_documents._try_file_lock(stream, exclusive=True)
        if unlock is None:
            raise StorageCompacting()
        try:
            yield
        finally:
            unlock()
