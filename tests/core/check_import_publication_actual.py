"""Measure import-report publication using an isolated populated library copy."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from yt_playlist.core import spotify_job_state
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import import_publication as publication
from yt_playlist.core.store import Store
from yt_playlist.web import spotify_import


def fingerprint(conn, owner, order):
    digest, count = hashlib.sha256(), 0
    for row in conn.execute('SELECT * FROM main.'+owner+' ORDER BY '+order):
        digest.update(json.dumps(tuple(row), ensure_ascii=False).encode())
        count += 1
    return count, digest.hexdigest()


def check(path, report_path):
    assert path.parent == report_path.parent == Path('/tmp') and path.name.startswith('tuneconsole-')
    report = {'status':'failed', 'database':str(path), 'python':sys.version, 'sqlite':sqlite3.sqlite_version,
        'dataset':'Existing isolated genuine v93 actual library. Original import setting presence is recorded below; the large report is a controlled fixture attributed to an existing actual account with 8192 ordered diagnostic entries.',
        'limits':'No native writes, providers, HTTP/runtime workers or full historical conversion. Original source restored afterward.',
        'batches':[], 'publications':{}}
    store, other = Store(path), Store(path)
    conn, owner = store.conn, publication.OWNER
    original = conn.execute('SELECT value FROM settings WHERE key=?', (owner.source,)).fetchone()
    original = tuple(original) if original else None
    protected = {'settings':'key', 'identities':'id', 'playlist_scopes':'ref', 'catalog_entries':'id',
                 'recordings':'id', 'youtube_items':'video_id'}
    before = {table:fingerprint(conn, table, order) for table, order in protected.items()}
    history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    reservations = []
    def trace(sql):
        if sql == 'BEGIN IMMEDIATE':
            reservations.append(time.monotonic())
    def finish():
        for _ in range(1000):
            reservations.clear()
            started = time.monotonic()
            progress = owner.worker.step(conn, batch_size=128)
            ended = time.monotonic()
            report['batches'].append({'seconds':ended-started,
                'writer_seconds':ended-reservations[-1] if reservations else 0})
            if progress.status == 'prepared':
                return progress.prepared_revision
        raise AssertionError('Import preparation did not finish')
    def publish(name, revision):
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
        owner.worker.install(conn)
        publish('installed', finish())
        report['original_present'] = original is not None
        report['original_bytes'] = len(original[0].encode()) if original and original[0] is not None else 0
        account = conn.execute('SELECT id FROM main.identities ORDER BY id LIMIT 1').fetchone()[0]
        value = {'status':'running', 'identity_id':account, 'phase':'import', 'started_at':2000000000,
            'finished_at':None, 'identity_label':'Controlled actual account report', 'report_schema':1,
            'dismissed':None, 'track_entries':8192, 'preview_token':None,
            'report':{'diagnostics':[{'position':index, 'source':'controlled', 'match':None,
                'original':['repeat', 'repeat', index]} for index in range(8192)]}}
        raw = json.dumps(value, indent=2)
        report['controlled_original_bytes'] = len(raw.encode())
        store.settings.set_setting(owner.source, raw)
        revision = finish()
        owner.install(conn)
        with conn:
            conn.execute('CREATE TRIGGER controlled_import_interrupt BEFORE INSERT ON '+owner.tables[1]+
                         " BEGIN SELECT RAISE(ABORT,'controlled import interruption'); END")
        try:
            owner.publish(conn, revision=revision)
        except sqlite3.IntegrityError as exc:
            assert 'controlled import interruption' in str(exc)
        else:
            raise AssertionError('Publication interruption was ignored')
        assert not conn.execute('SELECT 1 FROM '+owner.tables[0]+' WHERE revision=?', (revision,)).fetchone()
        with conn:
            conn.execute('DROP TRIGGER controlled_import_interrupt')
        def racing(connection, header, described):
            result = publication.verify_files(connection, header, described)
            with other.conn:
                other.conn.execute('UPDATE settings SET value=value WHERE key=?', (owner.source,))
            return result
        try:
            replace(owner, verify_files=racing).publish(conn, revision=revision)
        except MigrationError as exc:
            assert 'changed' in str(exc) or 'reconciled' in str(exc)
        else:
            raise AssertionError('Stale import publication was accepted')
        publish('controlled-8192', finish())
        assert other.settings.get_setting(owner.source) == raw
        assert spotify_import.load_job(other)['account_id'] == account
        Path(owner.capture(conn)['recovery_filename']).unlink()
        def failed(*args):
            assert not conn.in_transaction
            raise OSError('controlled import recovery failure')
        try:
            replace(owner.worker, repair_files=failed).step(conn, retry=True)
        except OSError:
            pass
        failed_progress = owner.worker.progress(conn)
        assert failed_progress.status == 'failed'
        started = time.monotonic()
        store.init_schema(defer_metadata=True)
        regular.migrate(conn)
        report['installed_ddl_while_failed_seconds'] = time.monotonic()-started
        assert owner.worker.progress(conn) == failed_progress
        owner.worker.step(conn, retry=True)
        publish('recovered-8192', owner.worker.progress(conn).prepared_revision)
        updated = {**spotify_import.load_job(other), 'status':'done', 'finished_at':2000000010}
        started = time.monotonic()
        spotify_import.save_job(store, updated)
        report['compatible_import_worker_save_seconds'] = time.monotonic()-started
        assert owner.selected(other.conn) is None
        saved = json.loads(other.settings.get_setting(owner.source))
        assert saved['identity_id'] == account and 'account_id' not in saved
        publish('continuing-import-save', finish())
        assert spotify_import.load_job(other)['finished_at'] == 2000000010
        regular.validate(conn)
        report.update(status='passed', interrupted_publication_rolled_back=True,
            stale_publication_rejected=True, failed_recovery_retried_explicitly=True)
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        conn.set_trace_callback(None)
        conn.rollback()
        try:
            with conn:
                if original is not None:
                    conn.execute('INSERT INTO settings(key,value) VALUES (?,?) '
                        'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (owner.source, original[0]))
                else:
                    conn.execute('DELETE FROM settings WHERE key=?', (owner.source,))
            assert before == {table:fingerprint(conn, table, order) for table, order in protected.items()}
            assert history == list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
            report['source_references_history_preserved_after_restore'] = True
        finally:
            report_path.write_text(json.dumps(report, indent=2))
            other.conn.close()
            conn.close()
    print(json.dumps({key:value for key, value in report.items() if key != 'batches'}, indent=2))


if __name__ == '__main__':
    check(Path(sys.argv[1]), Path(sys.argv[2]))
