"""Read-only candidate construction and recoverable closed-file installation.

The original is retained after installation. The caller
selects either installed-format compaction or the admitted normalized rebuild.
"""
from contextlib import closing, contextmanager, ExitStack
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
import uuid

from .. import db_handles, db_space, policy_documents, policy_recovery
from . import build, files, install


def _tree_bytes(path):
    stats = [file.stat() for file in path.rglob('*') if file.is_file()]
    return dict(files=len(stats),bytes=sum(stat.st_size for stat in stats),
                allocated_bytes=sum(getattr(stat,'st_blocks',0)*512 for stat in stats))


def _same_filesystem(left,right):
    return left.stat().st_dev==right.stat().st_dev


@contextmanager
def _files(conn):
    """Use the same finite file owners and lifetime fences as verified recovery."""
    from .. import (collection_recovery, group_documents, legacy_evidence, metadata_delivery_files,
                   metadata_destination_files, preparation_files, recommendation_outputs, saved_preparation_files,
                   sample_files)
    tables = policy_recovery._tables(conn)
    with ExitStack() as stack:
        from .. import file_owners
        stack.enter_context(file_owners.readers(conn.execute('PRAGMA database_list').fetchone()[2],
                                              getattr(conn, '_policy_memory_key', id(conn))))
        from .. import choice_evidence_files
        stack.enter_context(policy_documents.retained_reader(choice_evidence_files.directory(conn)))
        from .. import metadata_runtime_files, served_facts
        if served_facts.available(conn):
            stack.enter_context(policy_documents.retained_reader(served_facts.directory(conn)))
        if 'metadata_runtime_state' in tables:
            stack.enter_context(policy_documents.retained_reader(metadata_runtime_files.directory(conn)))
        for table, module in (('sample_attributes',sample_files),
                ('album_feedback',collection_recovery),('recommendation_policies',policy_documents),
                ('group_versions',group_documents),('prepared_outputs',recommendation_outputs),
                ('metadata_deliveries',metadata_delivery_files),
                ('published_metadata_destination_deliveries',metadata_destination_files),
                ('legacy_catalog_evidence',legacy_evidence)):
            if table in tables:
                stack.enter_context(policy_documents.retained_reader(module.directory(conn)))
        if saved_preparation_files.available(conn):
            stack.enter_context(policy_documents.retained_reader(saved_preparation_files.directory(conn)))
        for directory in preparation_files.directories(conn):
            stack.enter_context(policy_documents.retained_reader(directory))
        yield


def run(store, *, destination=None, rebuild=False, resume=False, scratch=None, progress=lambda phase, detail: None,
        cancelled=lambda: False):
    """Fence writes, build from the original, install and reopen runtime storage.

    A schema owner supplies ``destination`` for a direct normalized rebuild.
    ``rebuild=True`` selects the current contract from the write-fenced source.
    Without either selection this is installed-format compaction. Both routes
    share original-file custody, admission and installation. The direct builder
    reads the original through a read-only connection; its checkpoint rejects
    reuse after a source change. The original is retained after replacement.

    ``scratch`` is optional and may be on another filesystem. The last candidate
    is staged beside the destination database; existing immutable files stay put.
    """
    database = store._db_path
    if database is None:
        raise ValueError('Copy compaction requires a file database')
    rebuilding = rebuild or destination is not None
    if resume and (not rebuilding or scratch is not None):
        raise ValueError('Resume requires a destination and reuses the recorded scratch directory')
    began = time.monotonic()
    identity = uuid.uuid4().hex
    custody = database.parent/('.'+database.name+'.compaction-'+identity)
    work = (Path(scratch).resolve()/('tuneconsole-compact-'+identity)) if scratch else custody
    candidate, sibling = work/'copy.db', custody/'candidate.db'
    state = dict(format=1,database=str(database),id=identity,phase='building',work=str(work),
                 destination=str(custody),original=str(custody/'original.db'),
                 sibling=str(sibling),phases={})
    state['operation'] = 'rebuild' if rebuilding else 'compaction'
    if destination is not None:
        state['destination_version'] = destination.version
    file_fences = ExitStack()
    guard = None
    original_query_only = None
    def phase(name,detail=None):
        if cancelled():
            raise InterruptedError('Storage compaction interrupted')
        progress(name,detail or {})
    with install.owner(database):
        old = db_handles.state(database)
        if old and old['phase'] in db_handles.ACTIVE:
            raise RuntimeError('Interrupted compaction needs startup recovery before retry')
        if resume:
            if (not old or old['phase'] != 'failed' or old.get('failed_phase') != 'building'
                    or old.get('operation') != 'rebuild'
                    or not old.get('source_identity')):
                raise ValueError('No failed build for this destination is available to resume')
            if destination is None:
                from .destination import for_source
                destination = for_source(database, split=True)
            if old.get('destination_version') != destination.version:
                raise ValueError('No failed build for this destination is available to resume')
            state = old
            work, custody = Path(state['work']), Path(state['destination'])
            candidate, sibling = work/'copy.db', Path(state['sibling'])
            if not candidate.is_file():
                raise ValueError('The interrupted build has no candidate to resume')
            state.update(phase='building', resumed=True)
            state.pop('failed_phase', None)
            state.pop('error', None)
        try:
            with store._lock:
                # Admission setup belongs to the same recovery scope as the
                # copy: even a failed first state write must restore service.
                # A saved cleanup retry (or released-schema read-only mode)
                # must not prevent the rebuild that replaces that database.
                # Live maintenance and caller-owned transactions still block it.
                rebuild_recovery = (rebuilding and not getattr(store, '_activity_paused', False)
                                    and not getattr(store, '_evidence_maintenance', 0))
                if store.conn.in_transaction or store.activity_paused and not rebuild_recovery:
                    raise RuntimeError('Compaction needs idle write admission')
                original_query_only = store.conn.execute('PRAGMA query_only').fetchone()[0]
                store._maintenance_accounts = tuple(store.accounts.get_accounts())
                install.save(database,state)
                store.conn.execute('PRAGMA query_only=ON')
                store.activity_paused = True
                store.conn._storage_compacting = True
            guard = sqlite3.connect(database,timeout=.1)
            guard.row_factory = sqlite3.Row
            guard.set_progress_handler(lambda:int(cancelled()),1000)
            # Stabilize ordinary WAL custody before fingerprinting the source.
            # Closing the serving connection after an interrupted build must
            # not turn its unchanged, already-checkpointed rows into a new input.
            guard.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            guard.execute('BEGIN IMMEDIATE')
            # Existing file owners stay in place through either cutover.
            file_fences.enter_context(_files(guard))
            size = guard.execute('PRAGMA page_count').fetchone()[0]*guard.execute('PRAGMA page_size').fetchone()[0]
            wal = database.with_name(database.name+'-wal')
            state['source_wal_bytes'] = wal.stat().st_size if wal.exists() else 0
            same = _same_filesystem(work.parent,database.parent)
            # One fresh SQL candidate plus index sorting; external file bodies
            # stay borrowed and the original is retained by rename at cutover.
            state['capacity'] = dict(source_sql_bytes=size,same_filesystem=same,
                destination_growth_bytes=2*size+4*1024**2,
                scratch_growth_bytes=2*size+4*1024**2)
            db_space.require_space(database.parent,state['capacity']['destination_growth_bytes'])
            if not same:
                db_space.require_space(work.parent,state['capacity']['scratch_growth_bytes'])
            install.save(database,state)
            if not resume:
                custody.mkdir(mode=0o700)
                if work!=custody:
                    work.mkdir(mode=0o700)
            identity = build._source_identity(database)
            if resume and state['source_identity'] != identity:
                raise ValueError('Rebuild source changed; start a fresh job')
            state['source_identity'] = identity
            if rebuilding and destination is None:
                from .destination import for_source
                destination = for_source(database, split=True)
                state['destination_version'] = destination.version
            install.save(database,state)
            phase('copy')
            if destination is None:
                files.borrow(database, candidate)
                state['copy'] = build.build(database,candidate,progress=phase,cancelled=cancelled)
            else:
                state['copy'] = build.construct(database,candidate,destination,
                                               progress=phase,cancelled=cancelled,runtime_only=True)
                state['analytics_pending'] = bool(state['copy'].get('analytics_pending'))
            started = time.monotonic()
            state['phase'] = 'staging'
            install.save(database,state)
            phase('staging')
            same = _same_filesystem(work,custody)
            if same:
                os.replace(candidate,sibling)
            else:
                db_space.require_space(custody,candidate.stat().st_size)
                shutil.copyfile(candidate,sibling)
                if install.digest(candidate)!=install.digest(sibling):
                    raise ValueError('Cross-filesystem compaction staging changed candidate bytes')
            sibling.chmod(0o600)
            with closing(sqlite3.connect(sibling)) as conn:
                conn.row_factory = sqlite3.Row
                conn.set_progress_handler(lambda:int(cancelled()),1000)
                # The builder has checked logical preservation, integrity and
                # foreign keys. Admission must also recognize the target's
                # installed runtime contract before closing the source.
                build._format(conn)
            # The canonical filename continues using the same file owners.
            # Give the retained SQL original access to them for analytics too.
            files.borrow(database, Path(state['original']))
            install.sync(sibling)
            state['candidate_sha256'] = install.digest(sibling)
            state['phases']['staging_and_file_validation_seconds'] = time.monotonic()-started
            install.save(database,state)
            phase('closing')
            installing_guard, guard = guard, None
            install.replace(store, state, installing_guard, progress=phase)
            state['read_only_seconds'] = time.monotonic()-began
            state['destination_files'] = _tree_bytes(custody)
            state['scratch_files'] = _tree_bytes(work) if work!=custody else state['destination_files']
            state['installed_sql_bytes'] = database.stat().st_size
            state['installed_wal_bytes'] = wal.stat().st_size if wal.exists() else 0
            state['free_space_after_bytes'] = shutil.disk_usage(database.parent).free
            state['recoverable_original_bytes'] = Path(state['original']).stat().st_size
            state['physical_reclaimed_bytes'] = 0
            state['reclaimable_after_acceptance_bytes'] = state['recoverable_original_bytes']-state['installed_sql_bytes']
            install.save(database,state)
            return state
        except Exception as error:
            if original_query_only is None:
                raise
            state.setdefault('failed_phase', state['phase'])
            if guard is not None:
                guard.rollback()
                guard.close()
                guard = None
            with store._lock:
                if not getattr(store.conn, '_handle_closed', False):
                    state.update(phase='complete' if state['phase']=='complete' else 'failed',error=repr(error))
                    install.save(database,state)
            raise
        finally:
            if guard is not None:
                guard.close()
            file_fences.close()
            if not getattr(store.conn, '_handle_closed', False) and original_query_only is not None:
                with store._lock:
                    pending = db_handles.state(database)
                    if pending and pending['phase'] in db_handles.ACTIVE:
                        # If failure state itself could not be persisted,
                        # retain maintenance admission until startup recovery.
                        store.activity_paused = True
                        store.conn._storage_compacting = True
                    else:
                        store.activity_paused = False
                        required = getattr(store, '_rebuild_source_version', None) == 64
                        store.conn._storage_compacting = required
                        query_only = 1 if required else 0 if state['phase'] == 'complete' else original_query_only
                        store.conn.execute(f'PRAGMA query_only={query_only}')


def finish_analytics(store, *, progress=lambda phase, detail: None, cancelled=lambda: False):
    """Finish deferred data before admitting the recommendation/enrichment worker."""
    from ..priority import ServingLock
    from ..store import Store
    from .destination import for_source
    database = store._db_path
    with install.owner(database):
        state = db_handles.state(database)
        if not state or not state.get('analytics_pending'):
            return state
        original = Path(state['original'])
        if install.digest(original) != state['original_sha256']:
            raise ValueError('Retained analytics source changed')
        destination = for_source(original, split=True)
        if destination.version != state['destination_version']:
            raise ValueError('Analytics conversion code changed; inspect the pending rebuild')
        started = time.monotonic()
        # Keep the public Store's connection and lock available to requests.
        prepared = Store.open_current(database)
        # Separate connections still compete for SQLite's single writer. Share
        # request priority so each committed batch yields to foreground writes.
        prepared.priority = store.priority
        prepared._lock = ServingLock(store.priority)
        prepared._bind_repositories()
        def checkpoint():
            if cancelled():
                raise InterruptedError('Analytics copy interrupted')
            store.priority.checkpoint(cancelled)
        try:
            with store.priority.background(checkpoint), _files(prepared.conn):
                build.finish_analytics(original, prepared, destination, progress=progress, cancelled=cancelled)
            state['analytics_pending'] = False
            state['phases']['analytics_seconds'] = time.monotonic()-started
            install.save(database,state)
            with prepared.conn:
                prepared.conn.execute('DROP TABLE '+build.CHECKPOINT)
        finally:
            prepared.conn.close()
        return state


def main(argv=None):
    import argparse
    from ..store import Store
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database',type=Path)
    parser.add_argument('--scratch',type=Path,help='existing scratch directory; final custody stays beside the database')
    args = parser.parse_args(argv)
    if not args.database.is_file():
        parser.error('database must be an existing installed library')
    store = Store(args.database)
    try:
        # This is explicit operational maintenance. Ordinary startup does not
        # select compaction, and compaction never runs historical conversion.
        store.init_schema(defer_metadata=True)
        report = run(store,scratch=args.scratch,
                     progress=lambda phase,detail:print(json.dumps(dict(phase=phase,detail=detail)),flush=True))
        print(json.dumps(report,indent=2))
        return 0
    finally:
        store.conn.close()


if __name__=='__main__':
    raise SystemExit(main())
