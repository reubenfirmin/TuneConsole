"""Explicit read-only archive experiment; all preparation uses a derived v79 DB."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from tests.core.actual_publication_timing import WriterTimings
from yt_playlist.core.migrations import registry, runner, regular
from yt_playlist.core.preparation import metadata_conflicts as preparation, conflict_publication as publication
from yt_playlist.core.store import Store


REFERENCES = ('genre_family','genre_catalog','genre_alias','recordings','youtube_items','catalog_entries')


def fingerprint(conn, owner, order):
    digest = hashlib.sha256()
    for row in conn.execute('SELECT * FROM '+owner+' ORDER BY '+order):
        digest.update(json.dumps(tuple(row), ensure_ascii=False, allow_nan=False).encode())
        digest.update(b'\n')
    return digest.hexdigest()


def check(source, fixture, output):
    report = {'source':str(source),'fixture':str(fixture),'python':sys.version,'sqlite':sqlite3.sqlite_version,
        'scope':'Exact actual archived conflict inputs reconstructed into genuine v79 with current actual '
        'catalog/genre references. Historical inputs, not current operational conflicts or historical catalog chronology.'}
    actual = sqlite3.connect(source.resolve().as_uri()+'?mode=ro', uri=True)
    actual.row_factory = sqlite3.Row
    fixture.parent.mkdir(parents=True, exist_ok=True)
    store = Store(fixture)
    conn, timer = store.conn, WriterTimings()
    protected = {'schema_migrations':'version','metadata_legacy_records':'legacy_owner,legacy_id',
        **{owner:('label_key' if owner=='genre_alias' else 'key' if owner.startswith('genre_') else
                  'video_id' if owner=='youtube_items' else 'id') for owner in REFERENCES}}
    before = {owner:fingerprint(actual,owner,key) for owner,key in protected.items()}
    try:
        runner.migrate(conn, registry=registry.MIGRATIONS[:79])
        store.init_schema(defer_metadata=True)
        with conn:
            for owner in REFERENCES:
                columns = [row[1] for row in conn.execute('PRAGMA table_info('+owner+')')]
                native = {row[1] for row in actual.execute('PRAGMA table_info('+owner+')')}
                columns = [name for name in columns if name in native]
                key = protected[owner]
                updates = ','.join(name+'=excluded.'+name for name in columns if name!=key)
                conn.executemany('INSERT INTO '+owner+'('+','.join(columns)+') VALUES ('+
                    ','.join('?' for _ in columns)+') ON CONFLICT('+key+') DO UPDATE SET '+updates,
                    actual.execute('SELECT '+','.join(columns)+' FROM '+owner))
        count = 0
        began = time.monotonic()
        with conn:
            for row in actual.execute("SELECT d.raw_filename,d.raw_digest,d.raw_byte_size,d.raw_offset "
                    "FROM metadata_legacy_records r JOIN metadata_deliveries d ON d.id=r.delivery_id "
                    "WHERE r.legacy_owner='enrichment_conflict' ORDER BY r.legacy_id"):
                if not 0 < row['raw_byte_size'] <= preparation.MAX_ARCHIVE_BYTES:
                    raise AssertionError('Archived conflict exceeds experiment byte budget')
                with Path(row['raw_filename']).open('rb') as stream:
                    stream.seek(row['raw_offset'])
                    raw = stream.read(row['raw_byte_size'])
                assert hashlib.sha256(raw).hexdigest() == row['raw_digest']
                value = json.loads(raw)['value']
                assert value['format']=='legacy-metadata-record-1' and value['owner']=='enrichment_conflict'
                original = value['row']
                columns = preparation.COLUMNS
                conn.execute('INSERT INTO enrichment_conflict('+','.join(columns)+') VALUES ('+
                    ','.join('?' for _ in columns)+')', tuple(original[name] for name in columns))
                count += 1
        report['archive_loading_seconds'] = time.monotonic()-began
        report['archived_conflicts'] = count
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()
        original = fingerprint(conn,'enrichment_conflict','id')
        history = fingerprint(conn,'schema_migrations','version')
        timer.watch(conn)
        _,report['installation'] = timer.timed(lambda:preparation.JOB.install(conn))
        def measure(name, callback, done, maximum):
            began = time.monotonic()
            maximum_writer, maximum_batch, steps = 0,0,0
            for _ in range(maximum):
                value, timing = timer.timed(callback)
                maximum_writer = max(maximum_writer,timing['max_writer_seconds'])
                maximum_batch = max(maximum_batch,timing['seconds'])
                steps += 1
                if done(value):
                    report[name] = {'steps':steps,'seconds':time.monotonic()-began,
                        'max_writer_seconds':maximum_writer,'max_batch_seconds':maximum_batch}
                    return value
            raise AssertionError('Experiment exceeded bounded step count: '+name)
        progress = measure('preparation',lambda:preparation.JOB.step(conn,batch_size=128),
            lambda value:value.status=='prepared',count*3+100)
        stage = measure('staging',lambda:publication.OWNER.step(conn,batch_size=16),
            lambda value:value.complete,count+100)
        _,report['selection'] = timer.timed(lambda:publication.OWNER.publish(conn,stage=stage.id))
        def deny(action,owner,column,*rest):
            return sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_READ and owner=='enrichment_conflict' else sqlite3.SQLITE_OK
        expected = conn.execute('SELECT * FROM enrichment_conflict ORDER BY id')
        # Read the original independently before installing the SQL authorizer.
        source_rows = [dict(row) for row in expected]
        conn.set_authorizer(deny)
        try:
            for row, original_row in zip(conn.execute('SELECT * FROM '+publication.table(conn)+' ORDER BY id'),source_rows,strict=True):
                actual_row = dict(row)
                if head_kind := conn.execute('SELECT body_kind FROM '+preparation.HEAD+' WHERE conflict_id=? '
                        'AND generation=?',(row['id'],progress.input_revision)).fetchone():
                    if head_kind[0]=='list':
                        assert json.loads(actual_row.pop('candidates')) == json.loads(original_row['candidates'])
                        assert actual_row == {key:value for key,value in original_row.items() if key!='candidates'}
                    else:
                        assert actual_row == original_row
                else:
                    raise AssertionError('Missing prepared conflict')
        finally:
            conn.set_authorizer(None)
        _,report['regular_selected'] = timer.timed(lambda:regular.migrate(conn))
        def write():
            with conn:
                conn.execute('UPDATE enrichment_conflict SET updated_at=updated_at WHERE id=(SELECT min(id) FROM enrichment_conflict)')
        _,report['same_value_source_write'] = timer.timed(write)
        assert publication.OWNER.selected(conn) is None
        measure('reconcile',lambda:preparation.JOB.step(conn,batch_size=128),lambda value:value.status=='prepared',10)
        stage = measure('restaging',lambda:publication.OWNER.step(conn,batch_size=16),lambda value:value.complete,count+100)
        _,report['reselection'] = timer.timed(lambda:publication.OWNER.publish(conn,stage=stage.id))
        assert fingerprint(conn,'enrichment_conflict','id')==original
        assert fingerprint(conn,'schema_migrations','version')==history
        report.update(status='passed',exact_source_history=True,source_reads_denied=True,
            exact_candidate_order_and_decisions=True,
            candidates=conn.execute('SELECT count(*) FROM '+preparation.CANDIDATES).fetchone()[0])
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        timer.close()
        report['actual_sources_exact'] = before=={owner:fingerprint(actual,owner,key) for owner,key in protected.items()}
        actual.close()
        conn.close()
        output.write_text(json.dumps(report,indent=2)+'\n')


if __name__=='__main__':
    check(*(Path(value) for value in sys.argv[1:]))
