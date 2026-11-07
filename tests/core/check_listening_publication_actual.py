"""Exercise listening files using actual event values in an isolated v89 fixture."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from yt_playlist.core import recommendation_outputs as files
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import listening_outputs as preparation, listening_publication as publication
from yt_playlist.core.store import Store


def fingerprint(conn, owner):
    keys = sorted((row[5],row[1]) for row in conn.execute('PRAGMA main.table_info('+owner+')') if row[5])
    rows = [tuple(row) for row in conn.execute('SELECT * FROM main.'+owner+' ORDER BY '+','.join(key for _,key in keys))]
    return len(rows), hashlib.sha256(json.dumps(rows, default=lambda value:bytes(value).hex()).encode()).hexdigest()


def check(actual_path, fixture_path, report_path):
    assert actual_path.parent == report_path.parent == Path('/tmp') and actual_path.name.startswith('tuneconsole-')
    assert fixture_path.resolve().is_relative_to(Path.cwd()/'.pytest_cache') and not fixture_path.exists()
    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    report = {'status':'failed','python':sys.version,'sqlite':sqlite3.sqlite_version,
        'actual_database':str(actual_path),'fixture':str(fixture_path),'batches':[],'copies':[],'publications':{},
        'dataset':'Read-only existing isolated v93 actual-library copy. It has no native listening files. A genuine v89 fixture uses 3600 exact actual listening event rows in 600 controlled chunks, one controlled historical chunk, and a controlled manifest/edition. These are reconstructed display artifacts, not actual legacy files.',
        'limits':'No native writes, migrations, restart, providers, HTTP or complete historical conversion. The fixture retains exact actual event values without admitting identities. Native display equivalence is covered separately by genuine predecessor tests.'}
    actual = sqlite3.connect(actual_path.resolve().as_uri()+'?mode=ro', uri=True)
    actual.row_factory = sqlite3.Row
    protected = ('listening_output_files','play_events','settings','schema_migrations')
    before = {owner:fingerprint(actual, owner) for owner in protected}
    assert actual.execute('SELECT count(*) FROM listening_output_files').fetchone()[0] == 0
    events = list(actual.execute('SELECT * FROM main.play_events ORDER BY id LIMIT 3600'))
    assert len(events) == 3600
    columns = list(events[0].keys())
    chunks = {}
    for offset in range(0,len(events),6):
        raw = json.dumps({'columns':columns, 'rows':[list(event) for event in events[offset:offset+6]], 'catalog':{}},
            ensure_ascii=False, indent=2)
        chunks[hashlib.sha256(raw.encode()).hexdigest()[:24]] = raw
    historical = json.dumps({'columns':columns,'rows':[],'catalog':{}})
    historic_key = hashlib.sha256(historical.encode()).hexdigest()[:24]
    refs = [{'key':key,'from':'2026-01-01','until':'2026-10-31','bytes':len(raw.encode())} for key,raw in chunks.items()]
    documents = {'listening_analytics':json.dumps({'schema':1,'generation':'a'*24,'timezone':'America/Costa_Rica',
        'built_at':123.5,'chunks':refs}, indent=2), 'listening_edition':'{ "text": "Controlled retained edition 🎵" }',
        **{'listening_chunk:'+key:raw for key,raw in chunks.items()}, 'listening_chunk:'+historic_key:historical}
    report.update(actual_native_files=0,actual_event_rows=len(events),controlled_chunks=len(chunks),
        controlled_historical_chunks=1,controlled_source_bytes=sum(len(raw.encode()) for raw in documents.values()))
    store, second = Store(fixture_path), None
    reservations = []
    def trace(sql):
        if sql == 'BEGIN IMMEDIATE':
            reservations.append(time.monotonic())
    def timed(function):
        reservations.clear()
        started = time.monotonic()
        result = function()
        ended = time.monotonic()
        return result, {'seconds':ended-started,'writer_seconds':ended-reservations[-1] if reservations else 0}
    def prepare():
        preparation.JOB.install(store.conn)
        for _ in range(3000):
            current, duration = timed(lambda:preparation.JOB.step(store.conn, batch_size=32))
            report['batches'].append(duration)
            if current.status == 'prepared':
                return current
        raise AssertionError('Listening fixture preparation did not finish')
    def stage():
        for _ in range(100):
            current, duration = timed(lambda:publication.OWNER.step(store.conn, batch_size=32))
            report['copies'].append(duration)
            if current.complete:
                return current
        raise AssertionError('Listening fixture map did not finish')
    def publish(label, selected):
        _, duration = timed(lambda:publication.OWNER.publish(store.conn, stage=selected.id))
        report['publications'][label] = duration
        assert publication.OWNER.selected(second.conn) == selected.id
    try:
        runner.migrate(store.conn, registry=registry.MIGRATIONS[:89])
        store.init_schema(defer_metadata=True)
        with store.conn:
            store.conn.executemany('INSERT INTO main.rec_proposals VALUES (?,?,123.5)', documents.items())
        history = list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
        second = Store(fixture_path)
        second.init_schema(defer_metadata=True)
        store.conn.set_trace_callback(trace)
        second.conn.set_trace_callback(trace)
        prepare()
        first = publication.OWNER.step(store.conn, batch_size=32)
        assert first.copied == 32 and not first.complete
        with store.conn:
            store.conn.execute('CREATE TRIGGER controlled_listening_copy BEFORE INSERT ON '+publication.OWNER.table('map')+
                " BEGIN SELECT RAISE(ABORT,'controlled listening copy'); END")
        try:
            publication.OWNER.step(second.conn, batch_size=32)
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError('Listening map interruption ignored')
        assert publication.OWNER.stage(store.conn, first.id) == first
        with store.conn:
            store.conn.execute('DROP TRIGGER controlled_listening_copy')
        resumed = publication.OWNER.step(second.conn, batch_size=32)
        assert resumed.id == first.id and resumed.copied == 64
        current = stage()
        with store.conn:
            store.conn.execute('CREATE TRIGGER controlled_listening_cutover BEFORE INSERT ON '+publication.OWNER.table('head')+
                " BEGIN SELECT RAISE(ABORT,'controlled listening cutover'); END")
        try:
            publication.OWNER.publish(store.conn, stage=current.id)
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError('Listening cutover interruption ignored')
        assert not store.conn.execute('SELECT 1 FROM '+publication.OWNER.table('generations')).fetchone()
        with store.conn:
            store.conn.execute('DROP TRIGGER controlled_listening_cutover')
        publish('derived-actual-values', current)
        def deny_source(operation, owner, column, *unused):
            return sqlite3.SQLITE_DENY if operation == sqlite3.SQLITE_READ and owner == 'rec_proposals' and column == 'payload' else sqlite3.SQLITE_OK
        with second._lock:
            second.conn.set_authorizer(deny_source)
            try:
                assert {surface:second.listening_analytics.encoded(surface) for surface in documents} == documents
                assert {surface:second.rec_proposals._proposal_json(surface) for surface in documents} == documents
            finally:
                second.conn.set_authorizer(None)
        verify = type(publication.OWNER).verify
        def after(self, connection, identity):
            result = verify(self, connection, identity)
            with second.conn:
                second.listening_analytics.publish('{"text":"Continuing edition"}', 200, 'listening_edition')
            return result
        type(publication.OWNER).verify = after
        try:
            try:
                publication.OWNER.publish(store.conn, stage=current.id)
            except MigrationError:
                pass
            else:
                raise AssertionError('Stale listening cutover accepted')
        finally:
            type(publication.OWNER).verify = verify
        assert publication.OWNER.selected(store.conn) is None
        prepare()
        current = stage()
        publish('reconciled-race', current)
        header = publication.OWNER.capture(store.conn, current.id, 'listening_analytics')
        captured = {owner:list(map(tuple, store.conn.execute('SELECT * FROM '+owner))) for owner in (preparation.HEAD,preparation.REFS)}
        Path(header['filename']).unlink()
        assert second.listening_analytics.encoded() is None
        started = time.monotonic()
        second.init_schema(defer_metadata=True)
        report['regular_admission_missing_file_seconds'] = time.monotonic()-started
        publication.repair(store.conn, 'listening_analytics')
        assert captured == {owner:list(map(tuple, store.conn.execute('SELECT * FROM '+owner))) for owner in captured}
        publish('recovered-file', current)
        def write():
            with second.conn:
                second.listening_analytics.publish('{"text":"Continuing actual writer"}', 300, 'listening_edition')
        _, report['compatible_source_writer'] = timed(write)
        assert publication.OWNER.selected(store.conn) is None
        publish_file = files.publish
        def fail(*args):
            raise OSError('controlled listening file failure')
        files.publish = fail
        try:
            try:
                preparation.JOB.step(store.conn, batch_size=32)
            except OSError:
                pass
            else:
                raise AssertionError('Listening file failure ignored')
        finally:
            files.publish = publish_file
        failed = preparation.JOB.progress(store.conn)
        started = time.monotonic()
        second.init_schema(defer_metadata=True)
        regular.migrate(second.conn)
        report['regular_admission_failed_preparation_seconds'] = time.monotonic()-started
        assert preparation.JOB.progress(store.conn) == failed and failed.status == 'failed'
        preparation.JOB.step(store.conn, batch_size=32, retry=True)
        prepare()
        current = stage()
        publish('continuing-writer', current)
        assert second.rec_proposals.get_proposals('listening_edition') == {'text':'Continuing actual writer'}
        assert history == list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
        report.update(status='passed',exact_originals_and_selected_chunk_edges_verified=True,
            preopened_readers_use_files=True,interrupted_copy_resumed=True,interrupted_cutover_rolled_back=True,
            stale_cutover_rejected=True,missing_file_repaired_without_sql_changes=True,
            failed_job_retried_explicitly=True,continuing_writes_reconciled=True)
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        assert before == {owner:fingerprint(actual, owner) for owner in protected}
        report['actual_files_events_settings_history_preserved'] = True
        report_path.write_text(json.dumps(report, indent=2))
        actual.close()
        store.conn.close()
        if second is not None:
            second.conn.close()
    print(json.dumps({key:value for key,value in report.items() if key not in ('batches','copies')}, indent=2))


if __name__ == '__main__':
    check(*(Path(argument) for argument in sys.argv[1:]))
