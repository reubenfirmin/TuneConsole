"""Measure sync report/stamp publication on an existing isolated library copy."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from yt_playlist.core import sync_state
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import library_sync as preparation, library_sync_publication as publication
from yt_playlist.core.store import Store
from yt_playlist.library import sync_outcomes


def fingerprint(conn, owner, order):
    digest = hashlib.sha256()
    count = 0
    for row in conn.execute('SELECT * FROM main.'+owner+' ORDER BY '+order):
        digest.update(json.dumps(tuple(row), ensure_ascii=False).encode())
        count += 1
    return count, digest.hexdigest()


def check(path, report_path):
    assert path.parent == report_path.parent == Path('/tmp') and path.name.startswith('tuneconsole-')
    report = {'status':'failed', 'database':str(path), 'python':sys.version, 'sqlite':sqlite3.sqlite_version,
        'dataset':'Sync report and all three stamps from an existing isolated genuine v93 actual library. Controlled report adds 8192 playlist cursors and an explicit unknown account; controlled stamp retains 60 KB of numeric whitespace.',
        'limits':'No native writes, HTTP/runtime workers, provider calls or full historical conversion. Original sources restored after controlled writes.',
        'batches':[], 'publications':{}}
    store, other = Store(path), Store(path)
    conn = store.conn
    keys = (sync_state.KEY, *sync_state.STAMPS)
    sources = dict(conn.execute('SELECT key,value FROM settings WHERE key IN (?,?,?,?)', keys))
    protected = {'settings':'key', 'identities':'id', 'playlist_scopes':'ref', 'catalog_entries':'id',
                 'recordings':'id', 'youtube_items':'video_id'}
    before = {owner:fingerprint(conn, owner, order) for owner, order in protected.items()}
    history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    reservations = []
    def trace(sql):
        if sql == 'BEGIN IMMEDIATE':
            reservations.append(time.monotonic())
    def finish(owner):
        for _ in range(1000):
            reservations.clear()
            started = time.monotonic()
            progress = owner.worker.step(conn, batch_size=3 if owner is publication.STAMPS else 128)
            ended = time.monotonic()
            report['batches'].append({'source':owner.source, 'seconds':ended-started,
                'writer_seconds':ended-reservations[-1] if reservations else 0})
            if progress.status == 'prepared':
                return progress.prepared_revision
        raise AssertionError('Sync preparation did not finish')
    def publish(name, owner, revision):
        reservations.clear()
        started = time.monotonic()
        owner.publish(conn, revision=revision)
        ended = time.monotonic()
        assert owner.selected(other.conn) == revision
        report['publications'][name] = {'revision':revision, 'seconds':ended-started,
            'writer_seconds':ended-reservations[-1]}
    try:
        store.init_schema(defer_metadata=True)
        other.init_schema(defer_metadata=True)
        conn.set_trace_callback(trace)
        for name, owner in publication.OWNERS.items():
            owner.worker.install(conn)
            publish('actual-'+name, owner, finish(owner))
        report['actual_report_bytes'] = len(sources[sync_state.KEY].encode())
        report['actual_cursors'] = publication.RESULT.capture(conn)['item_count']
        assert {key:other.settings.get_setting(key) for key in keys} == sources
        assert sync_state.read(other.conn) == json.loads(sources[sync_state.KEY])
        value = json.loads(sources[sync_state.KEY])
        account = next(iter(value['accounts']))
        resources = value['accounts'][account].setdefault('resources', {})
        playlists = resources.setdefault('playlists', {'status':'partial'})
        playlists['items'] = {'controlled-playlist-'+str(index):{'status':'partial', 'cursor':'exact-'+str(index)}
                              for index in range(8192)}
        value['accounts']['999999999'] = {'status':'failed', 'resources':{}}
        raw = json.dumps(value)
        store.settings.set_setting(sync_state.KEY, raw)
        publish('controlled-8192-result', publication.RESULT, finish(publication.RESULT))
        assert sync_state.read(other.conn) == value
        controlled_stamps = {'last_sync_at':' '*60000+'12.5', 'last_sync_attempt_at':'\t13.25\n', 'last_sync_outcome':'complete'}
        store.settings.set_values(controlled_stamps)
        revision = finish(publication.STAMPS)
        def interrupted():
            raise RuntimeError('controlled interruption after stamp rows')
        try:
            publication.STAMPS.publish(conn, revision=revision, clock=interrupted)
        except RuntimeError:
            pass
        else:
            raise AssertionError('Publication interruption was ignored')
        assert not conn.execute('SELECT 1 FROM '+publication.VALUES+' WHERE revision=?', (revision,)).fetchone()
        verify = publication.STAMPS.verify
        def racing(connection, revision):
            proof = verify(connection, revision)
            with other.conn:
                other.conn.execute("UPDATE main.settings SET value=value WHERE key='last_sync_at'")
            return proof
        publication.STAMPS.verify = racing
        try:
            publication.STAMPS.publish(conn, revision=revision)
        except MigrationError as exc:
            assert 'changed' in str(exc) or 'reconciled' in str(exc)
        else:
            raise AssertionError('Stale scalar publication was accepted')
        finally:
            publication.STAMPS.verify = verify
        publish('controlled-60kb-stamps', publication.STAMPS, finish(publication.STAMPS))
        assert {key:other.settings.get_setting(key) for key in controlled_stamps} == controlled_stamps
        header = publication.RESULT.capture(conn)
        filename = conn.execute('SELECT filename FROM '+preparation.PARTS+
            ' WHERE source=? AND generation=? LIMIT 1', (sync_state.KEY, header['generation'])).fetchone()[0]
        Path(filename).unlink()
        def failed(*args):
            assert not conn.in_transaction
            raise OSError('controlled sync recovery failure')
        try:
            replace(preparation.RESULT_JOB, repair_files=failed).step(conn, retry=True)
        except OSError:
            pass
        failed_progress = preparation.RESULT_JOB.progress(conn)
        assert failed_progress.status == 'failed'
        started = time.monotonic()
        store.init_schema(defer_metadata=True)
        regular.migrate(conn)
        report['installed_ddl_while_failed_seconds'] = time.monotonic()-started
        assert preparation.RESULT_JOB.progress(conn) == failed_progress
        assert publication.STAMPS.selected(conn) is not None
        preparation.RESULT_JOB.step(conn, retry=True)
        assert sync_state.read(other.conn) == value
        started = time.monotonic()
        sync_outcomes.begin_attempt(store, 2000000000)
        sync_outcomes.finish_attempt(store, json.loads(sources[sync_state.KEY]), 2000000000)
        report['compatible_sync_attempt_seconds'] = time.monotonic()-started
        assert publication.RESULT.selected(conn) is None
        assert publication.STAMPS.selected(conn) is None
        assert sync_state.read(other.conn)['last_attempt_at'] == 2000000000
        regular.validate(conn)
        report.update(status='passed', interruption_retried=True, stale_publication_rejected=True,
                      missing_file_recovered_explicitly=True)
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        conn.set_trace_callback(None)
        conn.rollback()
        try:
            with conn:
                for key in keys:
                    if key in sources:
                        conn.execute('UPDATE settings SET value=? WHERE key=?', (sources[key], key))
                    else:
                        conn.execute('DELETE FROM settings WHERE key=?', (key,))
            assert before == {owner:fingerprint(conn, owner, order) for owner, order in protected.items()}
            assert history == list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
            report['original_sources_references_history_preserved_after_restore'] = True
        finally:
            report_path.write_text(json.dumps(report, indent=2))
            other.conn.close()
            conn.close()
    print(json.dumps({key:value for key, value in report.items() if key != 'batches'}, indent=2))


if __name__ == '__main__':
    check(Path(sys.argv[1]), Path(sys.argv[2]))
