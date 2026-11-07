"""Reconstruct current actual native source evidence into a genuine v79 fixture.

This is current native evidence expressed in the compatible finding shape, not
an assertion about the library's historical v79 state. The source is read-only.
"""
import json
from pathlib import Path
import sqlite3
import sys
import time

from tests.core.actual_publication_timing import WriterTimings
from tests.core.check_conflict_publication_actual import REFERENCES, fingerprint
from yt_playlist.core import metadata_native
from yt_playlist.core.migrations import registry, runner, regular
from yt_playlist.core.preparation import metadata_evidence as preparation, metadata_publication as publication, metadata_destinations as destination
from yt_playlist.core.store import Store


def check(source, fixture, output, *, resume=False):
    assert fixture.resolve().is_relative_to(Path('.pytest_cache').resolve())
    report = {'source':str(source),'fixture':str(fixture),'python':sys.version,'sqlite':sqlite3.sqlite_version,
        'scope':'Current actual native field references, source caches and links reconstructed into genuine v79 '
        'compatible source/finding shapes with current catalog references. Recorded current delivery keys fill '
        'the old required provenance delivery_id; profile values discard that transport field. '
        'Not historical v79 chronology.'}
    actual = sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True)
    actual.row_factory = sqlite3.Row
    fixture.parent.mkdir(parents=True,exist_ok=True)
    store,timer = Store(fixture),WriterTimings()
    conn = store.conn
    protected = {'native_metadata_observations':'id','musical_source_observations':'source_key,field',
        'rec_musical_sources':'source_key','rec_musical_links':'source_key,recording_id'}
    before = {name:fingerprint(actual,name,order) for name,order in protected.items()}
    try:
        began = time.monotonic()
        if not resume:
            runner.migrate(conn,registry=registry.MIGRATIONS[:79])
            store.init_schema(defer_metadata=True)
            with conn:
                for owner in REFERENCES:
                    native = {row[1] for row in actual.execute('PRAGMA table_info('+owner+')')}
                    columns = [row[1] for row in conn.execute('PRAGMA table_info('+owner+')') if row[1] in native]
                    key = 'label_key' if owner=='genre_alias' else 'key' if owner.startswith('genre_') else 'video_id' if owner=='youtube_items' else 'id'
                    conn.executemany('INSERT INTO '+owner+'('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+
                        ') ON CONFLICT('+key+') DO UPDATE SET '+','.join(column+'=excluded.'+column for column in columns if column!=key),
                        actual.execute('SELECT '+','.join(columns)+' FROM '+owner))
                for row in actual.execute('SELECT DISTINCT o.id,d.input_entry_id,d.input_recording_id,d.provider,d.observed_at,d.delivery_key '
                        'FROM musical_source_observations s JOIN native_metadata_observations o ON o.id=s.observation_id '
                        'JOIN metadata_deliveries d ON d.id=o.delivery_id ORDER BY o.id'):
                    payload = metadata_native.read(actual,row['id'])
                    provenance = {key:value for key,value in payload.items() if key not in ('field','value')}
                    provenance.setdefault('delivery_id', row['delivery_key'])
                    conn.execute('INSERT INTO metadata_findings(id,entry_id,recording_id,field,provider,value_json,observed_at,provenance_json) '
                        'VALUES (?,?,?,?,?,?,?,?)',(row['id'],row['input_entry_id'],row['input_recording_id'],payload['field'],row['provider'],
                        json.dumps(payload['value'],ensure_ascii=False,allow_nan=False),row['observed_at'],json.dumps(provenance,ensure_ascii=False,allow_nan=False)))
                # Restore recorded caches/links after the genuine v50 trigger's
                # import projection. Equal values never establish these links.
                columns = [row[1] for row in actual.execute('PRAGMA table_info(rec_musical_sources)')]
                for row in actual.execute('SELECT * FROM rec_musical_sources ORDER BY source_key'):
                    raw = json.dumps(metadata_native.source_values(actual,row['source_key']),ensure_ascii=False,allow_nan=False)
                    conn.execute('INSERT INTO rec_musical_sources('+','.join(columns)+',payload) VALUES ('+
                        ','.join('?' for _ in range(len(columns)+1))+') ON CONFLICT(source_key) DO UPDATE SET '+
                        ','.join(column+'=excluded.'+column for column in (*columns,'payload') if column!='source_key'),(*row,raw))
                conn.execute('DELETE FROM rec_musical_links')
                columns = [row[1] for row in actual.execute('PRAGMA table_info(rec_musical_links)')]
                conn.executemany('INSERT INTO rec_musical_links('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',
                    actual.execute('SELECT '+','.join(columns)+' FROM rec_musical_links'))
        else:
            assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]==79
            store.init_schema(defer_metadata=True)
            report['resumed_existing_reconstruction'] = True
        report['reconstruction_seconds'] = time.monotonic()-began
        report['source_counts'] = {owner:conn.execute('SELECT count(*) FROM '+owner).fetchone()[0]
            for owner in ('rec_musical_sources','rec_musical_links','metadata_findings')}
        original = {name:fingerprint(conn,name,order) for name,order in
            {'schema_migrations':'version','metadata_findings':'id','rec_musical_sources':'source_key','rec_musical_links':'source_key,recording_id'}.items()}
        timer.watch(conn)
        preparation.JOB.install(conn)
        def measure(name, callback, done):
            began,maximum_writer,maximum_batch = time.monotonic(),0,0
            for number in range(10000):
                value,timing = timer.timed(callback)
                maximum_writer = max(maximum_writer,timing['max_writer_seconds'])
                maximum_batch = max(maximum_batch,timing['seconds'])
                if number % 500 == 0:
                    print(json.dumps({'phase':name,'batches':number+1,'seconds':time.monotonic()-began}),flush=True)
                if done(value):
                    report[name] = {'batches':number+1,'seconds':time.monotonic()-began,
                        'max_writer_seconds':maximum_writer,'max_batch_seconds':maximum_batch}
                    return value
            raise AssertionError('Native metadata experiment did not converge')
        measure('preparation',lambda:preparation.JOB.step(conn,batch_size=16),lambda value:value.status=='prepared')
        current = measure('read_staging',lambda:publication.step(conn,batch_size=16),lambda value:value.complete)
        current = measure('delivery_staging',lambda:destination.step(conn,identity=current.id,batch_size=16),lambda value:value.complete)
        _,report['selection'] = timer.timed(lambda:destination.publish(conn,identity=current.id))
        assert destination.selected(conn)==current.id
        expected = {key:json.loads(raw) for key,raw in conn.execute('SELECT source_key,payload FROM rec_musical_sources')}
        def deny(action,owner,column,*rest):
            return sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_READ and (owner=='metadata_findings' or
                owner=='rec_musical_sources' and column=='payload') else sqlite3.SQLITE_OK
        conn.set_authorizer(deny)
        try:
            began = time.monotonic()
            for index,(key,values) in enumerate(expected.items()):
                assert destination.source_values(conn,key)==values
                result = store.musical_profiles.source_input(key)
                if result:
                    assert result[0]['observations']==values
                if index%1000==0:
                    print(json.dumps({'phase':'selected_source_reads','sources':index+1,
                        'seconds':time.monotonic()-began}),flush=True)
            report['all_selected_source_reads_seconds'] = time.monotonic()-began
        finally:
            conn.set_authorizer(None)
        _,report['regular_selected'] = timer.timed(lambda:regular.migrate(conn))
        assert original=={name:fingerprint(conn,name,order) for name,order in
            {'schema_migrations':'version','metadata_findings':'id','rec_musical_sources':'source_key','rec_musical_links':'source_key,recording_id'}.items()}
        report.update(status='passed',exact_source_values=True,legacy_source_reads_denied=True)
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        timer.close()
        report['actual_native_sources_exact'] = before=={name:fingerprint(actual,name,order) for name,order in protected.items()}
        actual.close()
        conn.close()
        output.write_text(json.dumps(report,indent=2)+'\n')


if __name__=='__main__':
    check(*(Path(value) for value in sys.argv[1:4]),resume='--resume' in sys.argv[4:])
