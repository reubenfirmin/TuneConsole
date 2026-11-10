"""Check receipt cutover on an existing isolated library, never the native DB."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
from types import SimpleNamespace

from yt_playlist.core import workflow_settings
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import workflow_receipts as preparation, workflow_publication as publication
from yt_playlist.core.store import Store
from yt_playlist.library import playlist_gc_retry
from yt_playlist.library.spotify_library import _playlist_transfers


def fingerprint(conn, owner, order):
    digest, count = hashlib.sha256(), 0
    for row in conn.execute('SELECT * FROM main.'+owner+' ORDER BY '+order):
        digest.update(json.dumps(tuple(row), ensure_ascii=False).encode())
        count += 1
    return count, digest.hexdigest()


def check(path, report_path):
    assert path.parent == report_path.parent == Path('/tmp') and path.name.startswith('tuneconsole-')
    report = {'status':'failed', 'database':str(path), 'python':sys.version, 'sqlite':sqlite3.sqlite_version,
        'dataset':'Existing isolated genuine v93 library. All installed receipt sources are verified. Controlled receipts use one existing account, 4096 actual video identities repeated into 8192 nullable slots, 8192 ordered duplicate album fingerprints, and one deliberately missing account.',
        'limits':'No native writes, HTTP, providers, runtime workers, remote intents or full historical conversion. Controlled settings are removed afterward; immutable preparation custody remains.',
        'batches':[], 'maps':[], 'publications':{}}
    store, other = Store(path), Store(path)
    conn = store.conn
    protected = {'settings':'key', 'rec_proposals':'surface', 'identities':'id', 'playlist_scopes':'ref',
        'catalog_entries':'id', 'recordings':'id', 'youtube_items':'video_id'}
    before = {table:fingerprint(conn, table, order) for table, order in protected.items()}
    history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    originals = {name:list(map(tuple, conn.execute('SELECT key,value FROM settings WHERE '+owner.job._filter('settings')+' ORDER BY key')))
        for name, owner in publication.OWNERS.items()}
    controlled, seeded = {}, False
    reservations = []
    def trace(sql):
        if sql == 'BEGIN IMMEDIATE':
            reservations.append(time.monotonic())
    def timed(category, owner, function):
        reservations.clear()
        started = time.monotonic()
        result = function()
        ended = time.monotonic()
        report[category].append({'job':owner.name, 'seconds':ended-started,
            'writer_seconds':ended-reservations[-1] if reservations else 0})
        return result
    def prepare(owner):
        owner.worker.install(conn)
        for _ in range(3000):
            progress = timed('batches', owner, lambda:owner.worker.step(conn, batch_size=128))
            if progress.status == 'prepared':
                return
        raise AssertionError('Workflow preparation did not finish bounded workload')
    def stage(owner):
        for _ in range(1000):
            result = timed('maps', owner, lambda:owner.step(conn, batch_size=8))
            if result.complete:
                return result.id
        raise AssertionError('Workflow map did not finish')
    def publish(label, owner, identity):
        reservations.clear()
        started = time.monotonic()
        owner.publish(conn, stage=identity)
        ended = time.monotonic()
        assert owner.selected(other.conn) == identity
        report['publications'][label+':'+owner.name] = {'stage':identity, 'seconds':ended-started,
            'writer_seconds':ended-reservations[-1], 'documents':owner.stage(conn, identity).copied}
    try:
        store.init_schema(defer_metadata=True)
        other.init_schema(defer_metadata=True)
        conn.set_trace_callback(trace)
        report['installed_counts'] = {name:len(rows) for name, rows in originals.items()}
        report['installed_bytes'] = {name:sum(len(raw.encode()) if raw is not None else 0 for _, raw in rows)
            for name, rows in originals.items()}
        for name, owner in publication.OWNERS.items():
            prepare(owner)
            identity = stage(owner)
            publish('installed', owner, identity)
            for source, raw in originals[name]:
                assert other.settings.get_setting(source) == raw
        actual_gc = {key:json.loads(raw) for key, raw in originals['gc']}
        assert playlist_gc_retry.states(other) == actual_gc
        account = conn.execute('SELECT id FROM identities ORDER BY id LIMIT 1').fetchone()[0]
        videos = [row[0] for row in conn.execute('SELECT video_id FROM youtube_items ORDER BY video_id LIMIT 4096')]
        assert len(videos) == 4096
        slots = [None if index % 7 == 0 else video for index, video in enumerate(videos*2)]
        source = workflow_settings.PLAYLIST+':'+str(account)
        missing = workflow_settings.PLAYLIST+':9223372036854775807'
        gc_key = playlist_gc_retry.key(account, 'controlled-unadmitted-workflow')
        receipt = {'creation_phase':'creating', 'marker':'controlled-original-marker', 'complete':False,
            'order_verified':None, 'updated_at':None, 'last_run_added':0, 'source_video_ids':slots,
            'opaque':{'paid_result':['東京', 2, 2]}, workflow_settings.TOKEN:'original-opaque-revision'}
        gc_value = {'identity':account, 'ytm':'controlled-unadmitted-workflow', 'attempts':3,
            'last_attempt_at':100, 'next_retry_at':200, 'phase':'delete', 'error':'Original failure evidence'}
        controlled = {source:json.dumps({'controlled-fingerprint':receipt}, ensure_ascii=False, indent=2),
            missing:json.dumps({'unknown':{'creation_phase':'created', 'ytm_playlist_id':'controlled-unadmitted-remote'}}),
            workflow_settings.ALBUM:json.dumps(['same', 'same', 'last']*2730+['same', 'last']),
            gc_key:json.dumps(gc_value, ensure_ascii=False, indent=2)}
        assert not any(conn.execute('SELECT 1 FROM settings WHERE key=?', (key,)).fetchone() for key in controlled)
        with conn:
            conn.executemany('INSERT INTO settings VALUES (?,?)', controlled.items())
        seeded = True
        report['controlled_bytes'] = {key:len(raw.encode()) for key, raw in controlled.items()}
        for name, owner in publication.OWNERS.items():
            assert owner.selected(conn) is None
            prepare(owner)
            identity = stage(owner)
            with conn:
                conn.execute('CREATE TRIGGER controlled_actual_workflow_cutover BEFORE INSERT ON '+owner.table('head')+
                    " BEGIN SELECT RAISE(ABORT,'controlled cutover interruption'); END")
            try:
                owner.publish(conn, stage=identity)
            except sqlite3.IntegrityError as exc:
                assert 'controlled cutover interruption' in str(exc)
            else:
                raise AssertionError('Workflow cutover interruption was ignored')
            assert not conn.execute('SELECT 1 FROM '+owner.table('generations')+' WHERE revision=?', (identity,)).fetchone()
            with conn:
                conn.execute('DROP TRIGGER controlled_actual_workflow_cutover')
            publish('controlled', owner, identity)
        for key, raw in controlled.items():
            assert other.settings.get_setting(key) == raw
        assert _playlist_transfers(other, account) == (source, {'controlled-fingerprint':receipt})
        assert playlist_gc_retry.states(other) == {**actual_gc, gc_key:gc_value}
        owner = publication.OWNERS['playlists']
        identity = owner.selected(conn)
        header = owner.capture(conn, identity, source)
        Path(header['plan_filename']).unlink()
        for suffix, column in (('parts', 'filename'), ('receipts', 'recovery_filename')):
            Path(conn.execute('SELECT '+column+' FROM '+preparation.table('playlists', suffix)+
                ' WHERE source=? AND preparation_revision=? LIMIT 1', (source, header['preparation_revision'])).fetchone()[0]).unlink()
        started = time.monotonic()
        store.init_schema(defer_metadata=True)
        regular.migrate(conn)
        report['installed_ddl_with_missing_file_seconds'] = time.monotonic()-started
        publication.repair('playlists', conn, source)
        publish('repaired', owner, identity)
        raced = False
        def race(connection, generation):
            nonlocal raced
            publication.verify_document('playlists', connection, generation)
            if not raced:
                raced = True
                with other.conn:
                    other.conn.execute('UPDATE settings SET value=value WHERE key=?', (source,))
        try:
            replace(owner, verify_document=race).publish(conn, stage=identity)
        except MigrationError:
            pass
        else:
            raise AssertionError('Stale receipt publication was accepted')
        assert owner.selected(conn) is None
        prepare(owner)
        publish('reconciled-race', owner, stage(owner))
        playlist = SimpleNamespace(account_id=account, provider_playlist_id='controlled-unadmitted-workflow')
        started = time.monotonic()
        updated = playlist_gc_retry.begin(store, playlist, 300, gc_value)
        report['compatible_gc_writer_seconds'] = time.monotonic()-started
        gc = publication.OWNERS['gc']
        assert gc.selected(conn) is None
        assert owner.selected(conn) is not None and publication.OWNERS['albums'].selected(conn) is not None
        prepare(gc)
        publish('continuing-gc', gc, stage(gc))
        assert playlist_gc_retry.read(other, playlist) == updated
        transfers = _playlist_transfers(store, account)[1]
        transfers['controlled-fingerprint']['source_video_ids'][0] = videos[0]
        store.settings.set_setting(source, json.dumps(transfers, ensure_ascii=False))
        def fail(*args):
            preparation.copy('playlists', *args)
            raise OSError('controlled actual workflow publication failure')
        try:
            replace(owner.worker, copy=fail).step(conn, batch_size=128)
        except OSError as exc:
            assert 'controlled actual workflow publication failure' in str(exc)
        else:
            raise AssertionError('Injected workflow failure did not run')
        failed = owner.worker.progress(conn)
        assert failed.status == 'failed'
        started = time.monotonic()
        store.init_schema(defer_metadata=True)
        regular.migrate(conn)
        report['installed_ddl_while_failed_seconds'] = time.monotonic()-started
        assert owner.worker.progress(conn) == failed
        try:
            owner.worker.step(conn)
        except MigrationError:
            pass
        else:
            raise AssertionError('Failed workflow job retried implicitly')
        owner.worker.step(conn, retry=True)
        prepare(owner)
        publish('continuing-transfer', owner, stage(owner))
        assert _playlist_transfers(other, account)[1] == transfers
        assert not conn.execute('SELECT 1 FROM identities WHERE id=9223372036854775807').fetchone()
        assert not conn.execute("SELECT 1 FROM main.playlist_scopes WHERE ref GLOB 'controlled-unadmitted-*'").fetchone()
        report.update(status='passed', interrupted_cutover_rolled_back=True, stale_publication_rejected=True,
            preopened_readers_use_publications=True, real_gc_and_transfer_readers_verified=True,
            ordered_slots=8192, repeated_album_positions=8192, missing_identity_unadmitted=True,
            exact_files_recovered=True, failed_job_retried_explicitly=True)
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        conn.set_trace_callback(None)
        conn.rollback()
        try:
            if seeded:
                with conn:
                    conn.executemany('DELETE FROM settings WHERE key=?', ((key,) for key in controlled))
            assert before == {table:fingerprint(conn, table, order) for table, order in protected.items()}
            assert history == list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
            report['original_sources_references_history_preserved_after_restore'] = True
        finally:
            report_path.write_text(json.dumps(report, indent=2))
            other.conn.close()
            conn.close()
    print(json.dumps({key:value for key, value in report.items() if key not in ('batches', 'maps')}, indent=2))


if __name__ == '__main__':
    check(Path(sys.argv[1]), Path(sys.argv[2]))
