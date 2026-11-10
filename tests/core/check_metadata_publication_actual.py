"""Explicit actual-archive metadata experiment on a separate genuine v79 DB."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from tests.core.actual_publication_timing import WriterTimings
from tests.core.check_conflict_publication_actual import REFERENCES, fingerprint
from yt_playlist.core.migrations import registry, runner, regular
from yt_playlist.core.preparation import metadata_evidence as preparation, metadata_publication as publication
from yt_playlist.core.store import Store


def check(source, fixture, output):
    report = {'source':str(source),'fixture':str(fixture),'python':sys.version,'sqlite':sqlite3.sqlite_version,
        'scope':'All archived actual metadata inputs reconstructed into genuine v79 with current actual '
        'catalog/genre references and current selection-source references. Archived historical inputs, '
        'not current operational evidence or historical catalog chronology.'}
    actual = sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True)
    actual.row_factory = sqlite3.Row
    fixture.parent.mkdir(parents=True,exist_ok=True)
    store = Store(fixture)
    conn,timer = store.conn,WriterTimings()
    before = fingerprint(actual,'metadata_legacy_records','legacy_owner,legacy_id')
    try:
        runner.migrate(conn,registry=registry.MIGRATIONS[:79])
        store.init_schema(defer_metadata=True)
        with conn:
            for owner in REFERENCES:
                columns = [row[1] for row in conn.execute('PRAGMA table_info('+owner+')')]
                native = {row[1] for row in actual.execute('PRAGMA table_info('+owner+')')}
                columns = [column for column in columns if column in native]
                key = 'label_key' if owner=='genre_alias' else 'key' if owner.startswith('genre_') else 'video_id' if owner=='youtube_items' else 'id'
                conn.executemany('INSERT INTO '+owner+'('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+
                    ') ON CONFLICT('+key+') DO UPDATE SET '+','.join(column+'=excluded.'+column for column in columns if column!=key),
                    actual.execute('SELECT '+','.join(columns)+' FROM '+owner))
        counts = {}
        began = time.monotonic()
        with conn:
            for owner,columns in preparation.SOURCE_COLUMNS.items():
                count = 0
                if owner in ('recording_field_sources','item_field_sources'):
                    conn.executemany('INSERT INTO '+owner+'('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',
                        actual.execute('SELECT '+','.join(columns)+' FROM '+owner))
                    counts[owner] = conn.execute('SELECT count(*) FROM '+owner).fetchone()[0]
                    continue
                for row in actual.execute('SELECT d.raw_filename,d.raw_digest,d.raw_byte_size,d.raw_offset '
                        'FROM metadata_legacy_records r JOIN metadata_deliveries d ON d.id=r.delivery_id '
                        'WHERE r.legacy_owner=? ORDER BY r.legacy_id',(owner,)):
                    assert 0<row['raw_byte_size']<=8*preparation.MAX_SOURCE_BYTES
                    with Path(row['raw_filename']).open('rb') as stream:
                        stream.seek(row['raw_offset'])
                        raw = stream.read(row['raw_byte_size'])
                    assert hashlib.sha256(raw).hexdigest()==row['raw_digest']
                    value = json.loads(raw)['value']
                    assert value['format']=='legacy-metadata-record-1' and value['owner']==owner
                    original = value['row']
                    conn.execute('INSERT INTO '+owner+'('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',
                        tuple(original[column] for column in columns))
                    count += 1
                counts[owner] = count
                print(json.dumps({'loaded':owner,'rows':count}),flush=True)
        report.update(source_counts=counts,archive_loading_seconds=time.monotonic()-began)
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()
        protected = {'schema_migrations':'version',**{owner:','.join(keys) for owner,keys in preparation.KEYS.items()}}
        fixture_before = {owner:fingerprint(conn,owner,order) for owner,order in protected.items()}
        timer.watch(conn)
        _,report['installation'] = timer.timed(lambda:preparation.JOB.install(conn))
        def measure(name,callback,done,maximum):
            began = time.monotonic()
            maximum_writer,maximum_batch = 0,0
            for index in range(maximum):
                value,timing = timer.timed(callback)
                maximum_writer = max(maximum_writer,timing['max_writer_seconds'])
                maximum_batch = max(maximum_batch,timing['seconds'])
                if (index+1)%5000==0:
                    print(json.dumps({'phase':name,'steps':index+1,'seconds':time.monotonic()-began}),flush=True)
                if done(value):
                    report[name] = {'steps':index+1,'seconds':time.monotonic()-began,
                        'max_writer_seconds':maximum_writer,'max_batch_seconds':maximum_batch}
                    return value
            raise AssertionError('Experiment exceeded bounded step count: '+name)
        measure('preparation',lambda:preparation.JOB.step(conn,batch_size=16),lambda value:value.status=='prepared',sum(counts.values())+100)
        current = measure('staging',lambda:publication.step(conn,batch_size=16),lambda value:value.complete,sum(counts.values())+100)
        _,report['selection'] = timer.timed(lambda:publication.publish(conn,identity=current.id))
        for owner,columns in preparation.SOURCE_COLUMNS.items():
            order = ','.join(preparation.KEYS[owner])
            expected = fingerprint(conn,owner,order)
            def deny(action,table,column,*rest):
                return sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_READ and table==owner else sqlite3.SQLITE_OK
            conn.set_authorizer(deny)
            try:
                assert fingerprint(conn,publication.read_table(conn,owner),order)==expected
            finally:
                conn.set_authorizer(None)
        _,report['regular_selected'] = timer.timed(lambda:regular.migrate(conn))
        report['regular_history'] = list(map(tuple,conn.execute('SELECT version,name FROM schema_changes ORDER BY version')))
        def write():
            with conn:
                conn.execute('UPDATE enrichment_provider_state SET at=at WHERE (recording_id,provider)='
                    '(SELECT recording_id,provider FROM enrichment_provider_state LIMIT 1)')
        _,report['same_value_source_write'] = timer.timed(write)
        assert publication.selected(conn) is None
        measure('reconcile',lambda:preparation.JOB.step(conn,batch_size=16),lambda value:value.status=='prepared',100)
        current = measure('restaging',lambda:publication.step(conn,batch_size=16),lambda value:value.complete,sum(counts.values())+100)
        _,report['reselection'] = timer.timed(lambda:publication.publish(conn,identity=current.id))
        assert fixture_before=={owner:fingerprint(conn,owner,order) for owner,order in protected.items()}
        report.update(status='passed',exact_sources_history=True,source_reads_denied=True)
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        timer.close()
        report['actual_archive_exact'] = before==fingerprint(actual,'metadata_legacy_records','legacy_owner,legacy_id')
        actual.close()
        conn.close()
        output.write_text(json.dumps(report,indent=2)+'\n')


if __name__=='__main__':
    check(*(Path(value) for value in sys.argv[1:]))
