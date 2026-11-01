"""Read-only candidate construction and recoverable closed-file installation.

Recovery points and the original are retained after installation. No native
operation is selected merely by importing this module or starting the app.
"""
from contextlib import closing, contextmanager, ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
import uuid

from .. import db_handles, db_space, policy_documents, policy_recovery
from . import build, install


def _owned_files(conn):
    """Inventory concrete recovery owners, including borrowed roots and plan pages."""
    from .. import (album_description_files, collection_recovery, group_documents, legacy_evidence,
                   metadata_delivery_files, metadata_destination_files, preparation_files,
                   recommendation_outputs, saved_preparation_files)
    tables = policy_recovery._tables(conn)
    paths = set()
    from .. import metadata_runtime_files, served_facts, choice_evidence_files
    paths.update(path.resolve(strict=True) for path in choice_evidence_files.references(conn))
    for row in served_facts.references(conn):
        paths.add((served_facts.directory(conn) / served_facts.relative(row['id'])).resolve(strict=True))
    for kind, owner, revision, required in metadata_runtime_files.references(conn):
        path = metadata_runtime_files.directory(conn) / metadata_runtime_files.relative(kind, owner, revision)
        if required or path.exists():
            paths.add(path.resolve(strict=True))
    def add(filename):
        if filename is not None:
            paths.add(Path(filename).resolve(strict=True))
    columns = [('sample_attributes','filename'), ('audio_annotations','model_output_filename'),
               ('recommendation_policies','configuration_filename'),
               ('recommendation_policy_bodies','configuration_filename'),
               ('action_backups','retained_filename')]
    columns.extend((table,owner[1]) for table,owner in album_description_files.OWNERS.items())
    columns.extend((table,'filename') for table in legacy_evidence.DOMAINS.values())
    for table,column in columns:
        if table in tables:
            for filename, in conn.execute('SELECT DISTINCT '+column+' FROM '+table+' WHERE '+column+' IS NOT NULL'):
                add(filename)
    for table,module,position in (('album_feedback',collection_recovery,3),
            ('group_versions',group_documents,3),('prepared_outputs',recommendation_outputs,4),
            ('metadata_deliveries',metadata_delivery_files,1),
            ('published_metadata_destination_deliveries',metadata_destination_files,1)):
        if table in tables:
            for reference in module.references(conn):
                add(reference[position])
    if saved_preparation_files.available(conn):
        for reference in saved_preparation_files.references(conn):
            add(reference[4])
    graphs = set()
    def graph(reference):
        add(reference[0])
        identity = tuple(reference)
        if identity not in graphs:
            graphs.add(identity)
            preparation_files._graph(preparation_files._read(reference),graph)
        return reference
    for owner in preparation_files.references(conn):
        if owner['columns'][0]=='plan_filename':
            graph(owner['reference'])
        else:
            add(owner['reference'][0])
    queue = policy_recovery.playback_feedback_backup._queue(conn.execute('PRAGMA database_list').fetchone()[2])
    if queue.path.exists():
        add(queue.path)
        with closing(sqlite3.connect(queue.path.as_uri()+'?mode=ro',uri=True)) as feedback:
            for filename, in feedback.execute('SELECT filename FROM playback_feedback_inbox'):
                add(filename)
    stats = [path.stat() for path in paths]
    return dict(files=len(stats),bytes=sum(stat.st_size for stat in stats),
                allocated_bytes=sum(getattr(stat,'st_blocks',0)*512 or (stat.st_size+4095)//4096*4096 for stat in stats))


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


def _verify_backup(source, backup, *, cancelled=lambda:False):
    with closing(sqlite3.connect(backup.as_uri()+'?mode=ro',uri=True)) as copied, \
            closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as original:
        build._configure(copied,cancelled)
        build._configure(original,cancelled)
        copied.execute('BEGIN')
        original.execute('BEGIN')
        schema = "SELECT type,name,sql FROM main.sqlite_schema WHERE name NOT GLOB 'sqlite_*' ORDER BY name"
        if copied.execute(schema).fetchall() != original.execute(schema).fetchall():
            raise ValueError('Recovery backup changed installed schema')
        for name, in original.execute("SELECT name FROM main.sqlite_schema WHERE type='table' "
                                       "AND name NOT GLOB 'sqlite_*'").fetchall():
            # A second connection keeps explicitly main-qualified preparation
            # guards in their valid schema. Stream in durable key order instead
            # of attaching a guarded installed database under another name.
            fields,keys = build._fields(original,name)
            generated = [row[1] for row in build._columns(original,name) if row[6] in (2,3)]
            query = 'SELECT '+','.join(map(build.quote,dict.fromkeys([*fields,*generated])))
            query += ' FROM '+build.quote(name)+' ORDER BY '+','.join(map(build.quote,keys))
            def digest(conn):
                result,count = hashlib.sha256(),0
                for row in conn.execute(query):
                    result.update(repr(tuple(row)).encode('utf8'))
                    result.update(b'\n')
                    count += 1
                return count,result.digest()
            if digest(copied)!=digest(original):
                raise ValueError('Recovery backup changed values or IDs: '+name)
        sequence = "SELECT 1 FROM main.sqlite_schema WHERE name='sqlite_sequence'"
        if copied.execute(sequence).fetchone() and copied.execute('SELECT * FROM sqlite_sequence ORDER BY name').fetchall() != \
                original.execute('SELECT * FROM sqlite_sequence ORDER BY name').fetchall():
            raise ValueError('Recovery backup changed sequence high-water marks')
        policy_recovery._verify(copied)
        if copied.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 64:
            from .source import admit
            admit(copied)
        else:
            build._format(copied)


def run(store, *, destination=None, resume=False, scratch=None, progress=lambda phase, detail: None,
        cancelled=lambda: False):
    """Fence writes, prepare verified custody, close handles, install and reopen.

    A schema owner supplies ``destination`` for a direct normalized rebuild.
    Without it this remains explicitly installed-format compaction. Both routes
    share recovery custody, admission and installation. The direct builder reads
    the immutable verified backup, so its batch bookmarks have a stable source.
    ``resume`` reuses a failed direct build only after proving that the serving
    database still equals that backup. A changed source needs a fresh job.

    ``scratch`` is optional and may be on another filesystem. The last candidate
    is staged beside the destination database; existing immutable files stay put.
    """
    database = store._db_path
    if database is None:
        raise ValueError('Copy compaction requires a file database')
    if resume and (destination is None or scratch is not None):
        raise ValueError('Resume requires a destination and reuses the recorded scratch directory')
    began = time.monotonic()
    identity = uuid.uuid4().hex
    custody = database.parent/('.'+database.name+'.compaction-'+identity)
    work = (Path(scratch).resolve()/('tuneconsole-compact-'+identity)) if scratch else custody
    candidate, sibling = work/'copy.db', custody/'candidate.db'
    state = dict(format=1,database=str(database),id=identity,phase='building',work=str(work),
                 destination=str(custody),original=str(custody/'original.db'),
                 backup=str(work/'verified-original.db'),sibling=str(sibling),phases={})
    state['operation'] = 'rebuild' if destination is not None else 'compaction'
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
                    or old.get('destination_version') != destination.version
                    or not old.get('backup_sha256')):
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
                released_source = getattr(store, '_rebuild_source_version', None) == 64
                if (store.conn.in_transaction or store.activity_paused and not (
                        released_source and destination is not None and not getattr(store, '_activity_paused', False))):
                    raise RuntimeError('Compaction needs idle write admission')
                original_query_only = store.conn.execute('PRAGMA query_only').fetchone()[0]
                store._maintenance_accounts = tuple(store.accounts.get_accounts())
                install.save(database,state)
                store.conn.execute('PRAGMA query_only=ON')
                store.activity_paused = True
                store.conn._storage_compacting = True
            guard = sqlite3.connect(database,timeout=.1)
            guard.set_progress_handler(lambda:int(cancelled()),1000)
            guard.execute('BEGIN IMMEDIATE')
            # SQL replacement preserves the installed path and file references.
            # Keep collection fenced through replacement instead of exporting,
            # restoring and rewriting every external-file owner a second time.
            file_fences.enter_context(_files(guard))
            size = guard.execute('PRAGMA page_count').fetchone()[0]*guard.execute('PRAGMA page_size').fetchone()[0]
            wal = database.with_name(database.name+'-wal')
            state['source_wal_bytes'] = wal.stat().st_size if wal.exists() else 0
            # The backup preserves free pages too. Candidate growth is bounded
            # conservatively by the original SQL image, before any allocation.
            state['source_files'] = _owned_files(guard)
            same = _same_filesystem(work.parent,database.parent)
            files = state['source_files']['allocated_bytes']
            # Include the independent backup files and an SQL-image allowance
            # for index sorting. Installed external files are reused unchanged.
            state['capacity'] = dict(source_sql_bytes=size,same_filesystem=same,
                destination_growth_bytes=(3*size+files if same else size)+4*1024**2,
                scratch_growth_bytes=3*size+files+4*1024**2)
            db_space.require_space(database.parent,state['capacity']['destination_growth_bytes'])
            if not same:
                db_space.require_space(work.parent,state['capacity']['scratch_growth_bytes'])
            install.save(database,state)
            if not resume:
                custody.mkdir(mode=0o700)
                if work!=custody:
                    work.mkdir(mode=0o700)
            phase('backup')
            started = time.monotonic()
            if resume:
                if install.digest(state['backup']) != state['backup_sha256']:
                    raise ValueError('The verified rebuild source changed; start a fresh job')
                _verify_backup(database,Path(state['backup']),cancelled=cancelled)
            else:
                with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as reader:
                    reader.row_factory = sqlite3.Row
                    reader.set_progress_handler(lambda:int(cancelled()),1000)
                    with _files(reader), policy_recovery.playback_feedback_backup.pinned(reader,Path(state['backup']),
                            backup_root=work/'original-files') as feedback:
                        manifests = policy_recovery._export_files(reader,Path(state['backup']),backup_root=work/'original-files')
                        if feedback is not None:
                            manifests['playback_feedback'] = feedback
                        with closing(sqlite3.connect(state['backup'])) as backup:
                            reader.backup(backup,pages=512,progress=lambda *_:phase('backup'))
                        _verify_backup(database,Path(state['backup']),cancelled=cancelled)
                state['backup_file_manifests'] = manifests
            Path(state['backup']).chmod(0o600)
            install.sync(Path(state['backup']))
            state['backup_sha256'] = install.digest(state['backup'])
            state['phases']['backup_seconds'] = time.monotonic()-started
            install.save(database,state)
            phase('copy')
            if destination is None:
                state['copy'] = build.build(database,candidate,progress=phase,cancelled=cancelled)
            else:
                state['copy'] = build.construct(Path(state['backup']),candidate,destination,
                                               progress=phase,cancelled=cancelled)
            started = time.monotonic()
            state['phase'] = 'staging'
            install.save(database,state)
            phase('staging')
            if _same_filesystem(work,custody):
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
                # Cross-filesystem staging checks its byte digest.
                # Required files were verified by the independent backup and
                # cannot be collected while the lifetime file fences are held.
                state['installed_files'] = _owned_files(conn)
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
