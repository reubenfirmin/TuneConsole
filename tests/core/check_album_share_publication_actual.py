"""Measure frozen album-choice publication on a disposable genuine v93 library."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import album_shares as preparation, album_share_publication as publication
from yt_playlist.core.store import Store


def fingerprint(conn, owner, order):
    digest, count = hashlib.sha256(), 0
    for row in conn.execute('SELECT * FROM main.'+owner+' ORDER BY '+order):
        digest.update(json.dumps(tuple(row), ensure_ascii=False).encode())
        count += 1
    return count, digest.hexdigest()


def check(path, report_path):
    assert path.parent == report_path.parent == Path('/tmp') and path.name.startswith('tuneconsole-')
    report = {'status':'failed', 'database':str(path), 'python':sys.version, 'sqlite':sqlite3.sqlite_version,
        'dataset':'Existing isolated genuine v93 library. All actual frozen album choices and provider aliases are checked. One actual share temporarily contains 8192 ordered choices from 4096 actual video identities, including repeated and explicitly unresolved choices.',
        'limits':'No native writes, HTTP, providers, runtime workers or full historical conversion. Installed tracklist completeness remains authoritative. A real cache writer, native upgrade and other supported predecessor formats are covered by focused fixtures. Sources restored afterward.',
        'batches':[], 'maps':[], 'publications':{}}
    store, other = Store(path), Store(path)
    conn, owner = store.conn, publication.OWNER
    protected = {'album_tracks':'id', 'album_fetch_state':'album_id', 'album_share_snapshots':'browse_id',
        'albums':'id', 'album_provider_ids':'provider,provider_id', 'settings':'key', 'identities':'id',
        'playlist_scopes':'ref', 'catalog_entries':'id', 'recordings':'id', 'youtube_items':'video_id'}
    before = {table:fingerprint(conn, table, order) for table, order in protected.items()}
    history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    source, title, raw = tuple(conn.execute('SELECT * FROM main.album_share_snapshots ORDER BY browse_id LIMIT 1').fetchone())
    touched, reservations = False, []
    def trace(sql):
        if sql == 'BEGIN IMMEDIATE':
            reservations.append(time.monotonic())
    def timed(category, function):
        reservations.clear()
        started = time.monotonic()
        result = function()
        ended = time.monotonic()
        report[category].append({'seconds':ended-started, 'writer_seconds':ended-reservations[-1] if reservations else 0})
        return result
    def prepare():
        preparation.JOB.install(conn)
        for _ in range(2000):
            if timed('batches', lambda:preparation.JOB.step(conn, batch_size=128)).status == 'prepared':
                return
        raise AssertionError('Album share preparation did not finish')
    def stage():
        for _ in range(1000):
            result = timed('maps', lambda:owner.step(conn, batch_size=16))
            if result.complete:
                return result.id
        raise AssertionError('Album share map did not finish')
    def publish(label, identity):
        reservations.clear()
        started = time.monotonic()
        owner.publish(conn, stage=identity)
        ended = time.monotonic()
        assert owner.selected(other.conn) == identity
        report['publications'][label] = {'stage':identity, 'seconds':ended-started,
            'writer_seconds':ended-reservations[-1], 'documents':owner.stage(conn, identity).copied}
    def update(next_title, next_raw):
        with conn:
            conn.execute('UPDATE main.album_share_snapshots SET title=?,video_ids_json=? WHERE browse_id=?', (next_title, next_raw, source))
    try:
        store.init_schema(defer_metadata=True)
        other.init_schema(defer_metadata=True)
        conn.set_trace_callback(trace)
        browse_ids = [row[0] for row in conn.execute("SELECT provider_id FROM main.album_provider_ids WHERE provider='youtube' ORDER BY provider_id")]
        baseline = {browse:other.collection.cached_album_share(browse) for browse in browse_ids}
        report['provider_aliases'] = len(browse_ids)
        report['readable_actual_shares'] = sum(value is not None for value in baseline.values())
        prepare()
        identity = stage()
        publish('installed', identity)
        def deny_original(operation, table, column, *unused):
            if operation == sqlite3.SQLITE_READ and table == 'album_share_snapshots' and column in ('title','video_ids_json'):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        other.conn.set_authorizer(deny_original)
        try:
            assert {browse:other.collection.cached_album_share(browse) for browse in browse_ids} == baseline
        finally:
            other.conn.set_authorizer(None)
        actual_members = 0
        for row in conn.execute('SELECT * FROM main.album_share_snapshots'):
            original = dict(row)
            assert owner.value(conn, identity, row['browse_id']) == original
            actual_members += len(json.loads(row['video_ids_json']))
        report['actual_members'] = actual_members
        videos = [row[0] for row in conn.execute('SELECT video_id FROM main.youtube_items ORDER BY video_id LIMIT 4096')]
        assert len(videos) == 4096
        choices = [video if index % 7 else '@controlled-unresolved-share-choice' for index, video in enumerate(videos*2)]
        controlled = json.dumps(choices, ensure_ascii=False, indent=2)+'\n'
        touched = True
        update('Controlled frozen 東京', controlled)
        assert owner.selected(conn) is None
        prepare()
        identity = stage()
        with conn:
            conn.execute('CREATE TRIGGER controlled_actual_album_share_cutover BEFORE INSERT ON '+owner.table('head')+
                " BEGIN SELECT RAISE(ABORT,'controlled cutover interruption'); END")
        try:
            owner.publish(conn, stage=identity)
        except sqlite3.IntegrityError as exc:
            assert 'controlled cutover interruption' in str(exc)
        else:
            raise AssertionError('Album share cutover interruption was ignored')
        assert not conn.execute('SELECT 1 FROM '+owner.table('generations')+' WHERE revision=?', (identity,)).fetchone()
        with conn:
            conn.execute('DROP TRIGGER controlled_actual_album_share_cutover')
        publish('controlled', identity)
        captured = owner.capture(conn, identity, source)
        assert owner.value(conn, identity, source) == {'browse_id':source,'title':'Controlled frozen 東京','video_ids_json':controlled}
        assert publication.selected_share(other.conn, identity, source) == {'title':'Controlled frozen 東京','video_ids':choices}
        expected = {'title':'Controlled frozen 東京','video_ids':choices} if other.collection.is_album_tracklist_complete(source) else None
        assert other.collection.cached_album_share(source) == expected
        assert not conn.execute("SELECT 1 FROM main.youtube_items WHERE video_id='@controlled-unresolved-share-choice'").fetchone()
        report['controlled_choices'] = len(choices)
        for filename in (captured['plan_filename'], conn.execute('SELECT filename FROM '+preparation.table('parts')+
                ' WHERE source=? AND preparation_revision=? LIMIT 1', (source, captured['preparation_revision'])).fetchone()[0]):
            Path(filename).unlink()
        started = time.monotonic()
        store.init_schema(defer_metadata=True)
        regular.migrate(conn)
        report['installed_ddl_with_missing_files_seconds'] = time.monotonic()-started
        publication.repair(conn, source)
        publish('repaired', identity)
        raced = False
        def race(connection, generation):
            nonlocal raced
            publication.verify_document(connection, generation)
            if not raced:
                raced = True
                with other.conn:
                    other.conn.execute('UPDATE main.album_share_snapshots SET title=title WHERE browse_id=?', (source,))
        try:
            replace(owner, verify_document=race).publish(conn, stage=identity)
        except MigrationError:
            pass
        else:
            raise AssertionError('Stale album share publication was accepted')
        assert owner.selected(conn) is None
        prepare()
        publish('reconciled-race', stage())
        started = time.monotonic()
        update('Continuing frozen choices', json.dumps(choices[:3]))
        report['compatible_source_writer_seconds'] = time.monotonic()-started
        def fail(*args):
            preparation.copy(*args)
            raise OSError('controlled actual album share failure')
        try:
            replace(preparation.JOB, copy=fail).step(conn, batch_size=128)
        except OSError as exc:
            assert 'controlled actual album share failure' in str(exc)
        else:
            raise AssertionError('Injected album share failure did not run')
        failed = preparation.JOB.progress(conn)
        started = time.monotonic()
        store.init_schema(defer_metadata=True)
        regular.migrate(conn)
        report['installed_ddl_while_failed_seconds'] = time.monotonic()-started
        assert preparation.JOB.progress(conn) == failed and failed.status == 'failed'
        preparation.JOB.step(conn, retry=True)
        prepare()
        identity = stage()
        publish('continuing', identity)
        assert publication.selected_share(other.conn, identity, source) == {'title':'Continuing frozen choices','video_ids':choices[:3]}
        report.update(status='passed', original_sql_read_denied=True, interrupted_cutover_rolled_back=True,
            stale_publication_rejected=True, exact_files_recovered=True, failed_job_retried_explicitly=True,
            preopened_readers_verified=True, continuing_writes_reconciled=True, completeness_gate_preserved=True)
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        conn.set_trace_callback(None)
        conn.rollback()
        try:
            if touched:
                update(title, raw)
            assert before == {table:fingerprint(conn, table, order) for table, order in protected.items()}
            assert history == list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
            report['original_sources_references_history_preserved_after_restore'] = True
        finally:
            report_path.write_text(json.dumps(report, indent=2))
            other.conn.close()
            conn.close()
    print(json.dumps({key:value for key, value in report.items() if key not in ('batches','maps')}, indent=2))


if __name__ == '__main__':
    check(Path(sys.argv[1]), Path(sys.argv[2]))
