"""Measure four ordered-document cutovers on an isolated actual-library copy."""
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from yt_playlist.core import album_playback_context
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import collection_feedback_publication, playback_publication
from yt_playlist.core.store import Store


def fingerprint(conn, owner, order):
    digest = hashlib.sha256()
    count = 0
    for row in conn.execute('SELECT * FROM main.'+owner+' ORDER BY '+order):
        digest.update(json.dumps(tuple(row), ensure_ascii=False).encode())
        count += 1
    return count, digest.hexdigest()


def check(path, report_path):
    assert path.parent == report_path.parent == Path('/tmp') and path.name.startswith('tuneconsole-')
    owners = {**collection_feedback_publication.OWNERS, **playback_publication.OWNERS}
    report = {'status':'failed', 'database':str(path), 'python':sys.version, 'sqlite':sqlite3.sqlite_version,
        'dataset':'Existing isolated genuine v93 actual library: four playback bindings and ten pending album uploads. Album feedback and authentication settings are absent. Large maps use controlled unknown keys; large playback lists repeat actual members.',
        'limits':'No native database writes, providers, HTTP/runtime workers or full historical conversion. Original sources restored after controls.',
        'sources':{}, 'batches':[], 'publications':{}}
    store, other = Store(path), Store(path)
    conn = store.conn
    keys = tuple(owner.source for owner in owners.values())
    sources = dict(conn.execute('SELECT key,value FROM settings WHERE key IN (?,?,?,?)', keys))
    protected = {'settings':'key', 'identities':'id', 'playlist_scopes':'ref', 'catalog_entries':'id',
                 'recordings':'id', 'youtube_items':'video_id',
                 'album_provider_ids':'provider,provider_id'}
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
            progress = owner.worker.step(conn, batch_size=128)
            ended = time.monotonic()
            report['batches'].append({'source':owner.source, 'seconds':ended-started,
                'writer_seconds':ended-reservations[-1] if reservations else 0})
            if progress.status == 'prepared':
                return progress.prepared_revision
        raise AssertionError('Document preparation did not finish')
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
        for name, owner in owners.items():
            raw = sources.get(owner.source)
            report['sources'][name] = {'present':owner.source in sources, 'bytes':len(raw.encode()) if raw is not None else 0}
            owner.worker.install(conn)
            publish('actual-'+name, owner, finish(owner))
            assert other.settings.get_setting(owner.source) == raw
        account = conn.execute('SELECT id FROM identities ORDER BY id LIMIT 1').fetchone()[0]
        binding = json.loads(sources['listening_playback_bindings'])[0]
        pending = json.loads(sources['listening_album_pending'])
        controls = {
            'album-feedback':{'controlled-release-'+str(index):{'kind':'more', 'at':index} for index in range(8192)},
            'account-auth':{str(account):'Known account', **{str(1000000+index):'Controlled unknown account' for index in range(8191)}},
            'bindings':[binding]*8192,
            'pending-album':{**pending, 'videos':[pending['videos'][0]]*8191+['controlled-unadmitted-upload']},
        }
        for name, owner in owners.items():
            raw = json.dumps(controls[name])
            store.settings.set_setting(owner.source, raw)
            revision = finish(owner)
            def interrupted():
                raise RuntimeError('controlled publication interruption')
            try:
                owner.publish(conn, revision=revision, clock=interrupted)
            except RuntimeError as exc:
                assert str(exc) == 'controlled publication interruption'
            else:
                raise AssertionError('Publication interruption was ignored')
            assert owner.selected(conn) is None
            if name == 'pending-album':
                def racing(connection, header, described):
                    owner.verify_rows(connection, header, described)
                    with other.conn:
                        other.conn.execute('UPDATE main.settings SET value=value WHERE key=?', (owner.source,))
                try:
                    replace(owner, verify_rows=racing).publish(conn, revision=revision)
                except MigrationError as exc:
                    assert 'reconciled' in str(exc) or 'changed' in str(exc)
                else:
                    raise AssertionError('Concurrent source update was ignored')
                revision = finish(owner)
                report['stale_publication_rejected'] = True
            publish('controlled-8192-'+name, owner, revision)
            assert other.settings.get_setting(owner.source) == raw
        owner = owners['pending-album']
        header = owner.capture(conn)
        filename = conn.execute('SELECT filename FROM '+owner.preparation.tables(owner.source)[1]+
            ' WHERE source=? AND generation=? LIMIT 1', (owner.source, header['generation'])).fetchone()[0]
        Path(filename).unlink()
        def failed(*args):
            assert not conn.in_transaction
            raise OSError('controlled missing-file recovery failure')
        try:
            replace(owner.worker, prepare=failed, repair_files=failed).step(conn, retry=True)
        except OSError:
            pass
        assert owner.worker.progress(conn).status == 'failed'
        failed_progress = owner.worker.progress(conn)
        started = time.monotonic()
        store.init_schema(defer_metadata=True)
        regular.migrate(conn)
        report['installed_ddl_while_failed_seconds'] = time.monotonic()-started
        assert owner.worker.progress(conn) == failed_progress
        owner.worker.step(conn, retry=True)
        assert len(album_playback_context.load(other.conn)['videos']) == 8192
        started = time.monotonic()
        store.rec_ledger.bind_choice_playback(binding['identity_id'], binding['playlist'], binding['video'],
            'controlled-later-binding', 9, binding['at']+1, 100)
        report['compatible_binding_write_seconds'] = time.monotonic()-started
        assert owners['bindings'].selected(conn) is None
        assert other.rec_ledger.choice_playback(binding['identity_id'], binding['playlist'], binding['video'],
            binding['at']+2, 100)['opportunity_id'] == 'controlled-later-binding'
        assert all('identity_id' in row and 'account_id' not in row for row in
                   json.loads(store.settings.get_setting('listening_playback_bindings')))
        with conn:
            album_playback_context.put(conn, None)
        assert owner.selected(conn) is None
        assert album_playback_context.load(other.conn) is None
        regular.validate(conn)
        report['final_progress'] = {name:asdict(owner.worker.progress(conn)) for name, owner in owners.items()}
        report['interruption_and_explicit_missing_file_recovery'] = 'passed'
        report['status'] = 'passed'
    finally:
        conn.rollback()
        try:
            with conn:
                for key in keys:
                    if key in sources:
                        conn.execute('UPDATE settings SET value=? WHERE key=?', (sources[key], key))
                    else:
                        conn.execute('DELETE FROM settings WHERE key=?', (key,))
            after = {owner:fingerprint(conn, owner, order) for owner, order in protected.items()}
            assert before == after
            assert history == list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
            report['sources_references_and_history_preserved_after_restore'] = True
        finally:
            report_path.write_text(json.dumps(report, indent=2))
            other.conn.close()
            conn.close()
    print(json.dumps({key:value for key, value in report.items() if key not in ('batches','final_progress')}, indent=2))


if __name__ == '__main__':
    check(Path(sys.argv[1]), Path(sys.argv[2]))
