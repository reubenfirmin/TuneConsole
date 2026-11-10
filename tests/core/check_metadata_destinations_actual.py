"""Explicit delivery publication over an existing disposable prepared v79 copy.

Does not clone or touch the native library. The companion metadata publication
experiment establishes the archived source reconstruction and original counts.
"""
import json
from pathlib import Path
import sqlite3
import sys
import time

from tests.core.actual_publication_timing import WriterTimings
from tests.core.check_conflict_publication_actual import fingerprint
from yt_playlist.core.migrations import regular
from yt_playlist.core.preparation import metadata_destinations as destination, metadata_evidence as preparation, metadata_publication as publication
from yt_playlist.core.store import Store


def check(path, output):
    path = path.resolve()
    assert path.is_relative_to(Path('.pytest_cache').resolve())
    report = {'fixture':str(path),'python':sys.version,'sqlite':sqlite3.sqlite_version,
        'scope':'Existing actual archived metadata reconstruction in genuine v79. '
        'Historical inputs with current catalog references, not current operational metadata. '
        'Any musical-source cache coverage is reported separately.'}
    store,timer = Store(path),WriterTimings()
    conn = store.conn
    try:
        store.init_schema(defer_metadata=True)
        protected = {'schema_migrations':'version',**{owner:','.join(keys) for owner,keys in preparation.KEYS.items()}}
        before = {owner:fingerprint(conn,owner,order) for owner,order in protected.items()}
        current = publication.stage(conn)
        assert current and current.complete
        timer.watch(conn)
        _,report['installation'] = timer.timed(lambda:destination.install(conn))
        source_count = current.copied
        started,maximum_writer,maximum_batch = time.monotonic(),0,0
        for number in range(source_count+100):
            result,timing = timer.timed(lambda:destination.step(conn,identity=current.id,batch_size=16))
            maximum_writer = max(maximum_writer,timing['max_writer_seconds'])
            maximum_batch = max(maximum_batch,timing['seconds'])
            if (number+1)%5000==0:
                print(json.dumps({'phase':'delivery_staging','batches':number+1,'seconds':time.monotonic()-started}),flush=True)
            if result.complete:
                break
        else:
            raise AssertionError('Delivery staging did not converge')
        report['staging'] = {'batches':number+1,'records':result.copied,'seconds':time.monotonic()-started,
            'max_writer_seconds':maximum_writer,'max_batch_seconds':maximum_batch}
        _,report['selection'] = timer.timed(lambda:destination.publish(conn,identity=current.id))
        assert destination.selected(conn)==current.id
        report['destination_counts'] = {name:conn.execute('SELECT count(*) FROM '+destination.table(name)+
            (' WHERE stage=?' if name!='deliveries' else ''),() if name=='deliveries' else (current.id,)).fetchone()[0]
            for name in ('deliveries','records','observations','native','choices','selections','hints','identifiers','fields')}
        report['musical_source_caches'] = conn.execute('SELECT count(*) FROM rec_musical_sources').fetchone()[0]
        def deny(action,owner,column,*rest):
            return sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_READ and owner in preparation.KEYS else sqlite3.SQLITE_OK
        conn.set_authorizer(deny)
        try:
            # Every opaque field value is recovered from the supplying delivery,
            # and its ordered field membership is the published relation.
            for owner in ('provider_attempts','enrichment_provider_state'):
                for key, in conn.execute('SELECT source_key FROM '+destination.table('records')+' WHERE stage=? AND owner=?',
                        (current.id,owner)):
                    assert destination.operational_fields(conn,owner,key) is not None
        finally:
            conn.set_authorizer(None)
        _,report['regular_selected'] = timer.timed(lambda:regular.migrate(conn))
        report['regular_history'] = list(map(tuple,conn.execute('SELECT version,name FROM schema_changes ORDER BY version')))
        assert before=={owner:fingerprint(conn,owner,order) for owner,order in protected.items()}
        report.update(status='passed',sources_and_numbered_history_exact=True,operational_source_reads_denied=True)
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        timer.close()
        conn.close()
        output.write_text(json.dumps(report,indent=2)+'\n')


if __name__=='__main__':
    check(*(Path(value) for value in sys.argv[1:]))
