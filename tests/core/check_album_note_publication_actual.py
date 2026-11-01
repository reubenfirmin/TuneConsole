"""Verify actual native note files and their values in an isolated genuine v83 fixture."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from yt_playlist.core import album_note_outputs, recommendation_outputs as files
from yt_playlist.core.migrations import MigrationError, registry, regular, runner
from yt_playlist.core.preparation import album_notes as preparation, album_note_publication as publication
from yt_playlist.core.store import Store


def fingerprint(conn, owner):
    keys = sorted((row[5], row[1]) for row in conn.execute('PRAGMA main.table_info('+owner+')') if row[5])
    rows = [dict(row) for row in conn.execute('SELECT * FROM main.'+owner+' ORDER BY '+','.join(name for _,name in keys))]
    return len(rows), hashlib.sha256(json.dumps(rows, sort_keys=True, default=lambda value:bytes(value).hex()).encode()).hexdigest()


def check(actual_path, fixture_path, report_path):
    assert actual_path.parent == report_path.parent == Path('/tmp') and actual_path.name.startswith('tuneconsole-')
    assert fixture_path.resolve().is_relative_to(Path.cwd()/'.pytest_cache') and not fixture_path.exists()
    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    report = {'status':'failed','python':sys.version,'sqlite':sqlite3.sqlite_version,'actual_database':str(actual_path),
        'fixture':str(fixture_path),'batches':[],'publications':{},
        'dataset':'Every native album-note file in the existing isolated v93 actual-library copy, plus a genuine v83 fixture populated with those exact note values and actual release/provider identities. The fixture expresses normalized genre keys as their retained display label for the genuine v83 genre column. Its legacy document is explicitly reconstructed controlled input, not an actual legacy artifact.',
        'limits':'The actual copy is read-only. No native writes, migrations, restart, providers, HTTP or full conversion. Controlled 128-note/8192-position output exercises the supported boundary. Paid descriptions and request owners remain outside publication.'}
    actual = sqlite3.connect(actual_path.resolve().as_uri()+'?mode=ro', uri=True)
    actual.row_factory = sqlite3.Row
    protected = ('album_note_outputs','album_note_requests','album_descriptions','albums','album_provider_ids','schema_migrations')
    before = {owner:fingerprint(actual, owner) for owner in protected}
    observed = album_note_outputs.read(actual)
    assert observed and publication.native(actual) and publication.selected(actual) is None
    report['actual_native_notes'] = len(observed)
    report['actual_note_bytes'] = actual.execute('SELECT sum(byte_size) FROM album_note_outputs').fetchone()[0]
    report['actual_paid_results'] = actual.execute('SELECT count(*) FROM album_descriptions').fetchone()[0]
    store = Store(fixture_path)
    other = None
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
        for _ in range(160):
            current, duration = timed(lambda:preparation.JOB.step(store.conn, batch_size=8))
            report['batches'].append(duration)
            if current.status == 'prepared':
                return current.prepared_revision
        raise AssertionError('Album-note preparation did not finish')
    def publish(label, revision):
        _, duration = timed(lambda:publication.publish(store.conn, revision=revision))
        assert publication.selected(other.conn) == revision
        report['publications'][label] = duration
    try:
        runner.migrate(store.conn, registry=registry.MIGRATIONS[:83])
        store.init_schema(defer_metadata=True)
        with store.conn:
            for owner in ('albums','album_provider_ids'):
                columns = [row[1] for row in store.conn.execute('PRAGMA main.table_info('+owner+')')]
                projection = ["coalesce((SELECT name FROM main.genre_catalog WHERE key=albums.genre_key),unresolved_genre) AS genre"
                    if owner == 'albums' and column == 'genre' else column for column in columns]
                store.conn.executemany('INSERT INTO main.'+owner+'('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',
                    [tuple(row) for row in actual.execute('SELECT '+','.join(projection)+' FROM main.'+owner)])
            raw = json.dumps(observed, ensure_ascii=False, indent=2)
            store.conn.execute("INSERT INTO main.rec_proposals VALUES ('album_notes',?,123.5)", (raw,))
        history = list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
        other = Store(fixture_path)
        other.init_schema(defer_metadata=True)
        store.conn.set_trace_callback(trace)
        other.conn.set_trace_callback(trace)
        revision = prepare()
        publish('actual-values', revision)
        assert other.rec_proposals.get_proposals('album_notes') == observed
        captured = store.conn.execute('SELECT * FROM '+preparation.HEAD+' WHERE generation=?', (revision,)).fetchone()
        assert preparation.read_original(captured) == raw
        def deny_source(operation, owner, column, *unused):
            return sqlite3.SQLITE_DENY if operation == sqlite3.SQLITE_READ and owner == 'rec_proposals' and column == 'payload' else sqlite3.SQLITE_OK
        with other._lock:
            other.conn.set_authorizer(deny_source)
            try:
                assert other.rec_proposals.get_proposals('album_notes') == observed
            finally:
                other.conn.set_authorizer(None)
        controlled = {f'controlled-{position}':dict(list(observed.values())[position % len(observed)]) for position in range(128)}
        controlled['controlled-0'] = {**controlled['controlled-0'],'positions':list(range(8192))}
        store.rec_proposals.put_proposals('album_notes', controlled, 200)
        first = preparation.JOB.step(store.conn, batch_size=8)
        assert first.status == 'copying'
        publish_file = files.publish
        def interrupted(*args):
            assert not store.conn.in_transaction
            raise KeyboardInterrupt('controlled note interruption')
        files.publish = interrupted
        try:
            try:
                preparation.JOB.step(store.conn, batch_size=8)
            except KeyboardInterrupt:
                pass
            else:
                raise AssertionError('Album-note copy interruption was ignored')
        finally:
            files.publish = publish_file
        assert preparation.JOB.progress(store.conn) == first
        resumed = preparation.JOB.step(other.conn, batch_size=8)
        assert resumed.processed == first.processed+8
        revision = prepare()
        with store.conn:
            store.conn.execute('CREATE TRIGGER controlled_note_cutover BEFORE INSERT ON '+publication.HEAD+
                " BEGIN SELECT RAISE(ABORT,'controlled note cutover'); END")
        try:
            publication.publish(store.conn, revision=revision)
        except sqlite3.IntegrityError as exc:
            assert 'controlled note cutover' in str(exc)
        else:
            raise AssertionError('Album-note cutover interruption was ignored')
        with store.conn:
            store.conn.execute('DROP TRIGGER controlled_note_cutover')
        assert not store.conn.execute('SELECT 1 FROM '+publication.GENERATIONS+' WHERE revision=?', (revision,)).fetchone()
        publish('controlled', revision)
        assert other.rec_proposals.get_proposals('album_notes') == controlled
        assert store.conn.execute('SELECT count(*) FROM '+preparation.FILES+' WHERE generation=? AND album_provider_id IS NOT NULL', (revision,)).fetchone()[0] == 0
        report['controlled_notes'], report['controlled_ordered_positions'] = len(controlled), 8192
        verify = publication.verify
        def after(connection, revision):
            result = verify(connection, revision)
            with other.conn:
                other.conn.execute("UPDATE main.album_provider_ids SET is_primary=is_primary WHERE provider='youtube' "
                    "AND provider_id=(SELECT min(provider_id) FROM main.album_provider_ids WHERE provider='youtube')")
            return result
        publication.verify = after
        try:
            try:
                publication.publish(store.conn, revision=revision)
            except MigrationError:
                pass
            else:
                raise AssertionError('Stale album-note cutover was accepted')
        finally:
            publication.verify = verify
        assert publication.selected(store.conn) is None
        revision = prepare()
        publish('reconciled-race', revision)
        header = store.conn.execute('SELECT * FROM '+preparation.HEAD+' WHERE generation=?', (revision,)).fetchone()
        rows = list(store.conn.execute('SELECT * FROM '+preparation.FILES+' WHERE generation=? ORDER BY position', (revision,)))
        Path(header['filename']).unlink()
        Path(rows[0]['filename']).unlink()
        started = time.monotonic()
        other.init_schema(defer_metadata=True)
        report['installed_ddl_with_missing_files_seconds'] = time.monotonic()-started
        publication.repair(store.conn)
        assert list(store.conn.execute('SELECT * FROM '+preparation.FILES+' WHERE generation=? ORDER BY position', (revision,))) == rows
        publish('recovered-files', revision)
        _, report['compatible_source_writer'] = timed(lambda:store.rec_proposals.publish_album_note('controlled-0', {'description':'Continuing display result'}, 300))
        assert publication.selected(store.conn) is None
        def fail(*args):
            raise OSError('controlled note file failure')
        files.publish = fail
        try:
            try:
                preparation.JOB.step(store.conn, batch_size=8)
            except OSError:
                pass
            else:
                raise AssertionError('Album-note file failure was ignored')
        finally:
            files.publish = publish_file
        failed = preparation.JOB.progress(store.conn)
        started = time.monotonic()
        other.init_schema(defer_metadata=True)
        regular.migrate(other.conn)
        report['installed_ddl_while_failed_seconds'] = time.monotonic()-started
        assert preparation.JOB.progress(store.conn) == failed and failed.status == 'failed'
        preparation.JOB.step(store.conn, batch_size=8, retry=True)
        revision = prepare()
        publish('continuing', revision)
        assert other.rec_proposals.get_proposals('album_notes')['controlled-0'] == {'description':'Continuing display result'}
        assert history == list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
        report.update(status='passed', native_values_and_exact_controlled_original_verified=True,
            preopened_reader_uses_files=True, interrupted_copy_resumed=True, interrupted_cutover_rolled_back=True,
            stale_publication_rejected=True, missing_files_recovered_without_sql_changes=True,
            failed_job_retried_explicitly=True, continuing_writes_reconciled=True)
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        assert before == {owner:fingerprint(actual, owner) for owner in protected}
        report['actual_native_notes_requests_paid_results_references_history_preserved'] = True
        report_path.write_text(json.dumps(report, indent=2))
        actual.close()
        if other is not None:
            other.conn.close()
        store.conn.close()
    print(json.dumps({key:value for key,value in report.items() if key != 'batches'}, indent=2))


if __name__ == '__main__':
    check(*(Path(argument) for argument in sys.argv[1:]))
