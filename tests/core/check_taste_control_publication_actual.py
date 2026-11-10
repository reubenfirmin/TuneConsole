"""Measure taste-control publication on an existing isolated actual-library copy."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import taste_controls as preparation, taste_control_publication as publication
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
        'dataset':'Existing isolated genuine v93 actual library. Actual checkpoint retains its original parameters and weights. Baseline and decisions are measured as installed, then controlled fixtures add 8192 ordered baseline entries, 8192 checkpoint axes, and 200 evidence-bearing decisions.',
        'limits':'No native writes, HTTP/runtime workers, provider calls or full historical conversion. Controlled sources are restored afterward.',
        'sources':{}, 'batches':[], 'publications':{}}
    store, other = Store(path), Store(path)
    conn = store.conn
    keys = tuple(publication.SOURCES)
    sources = dict(conn.execute('SELECT key,value FROM settings WHERE key IN (?,?,?)', keys))
    protected = {'settings':'key', 'identities':'id', 'playlist_scopes':'ref', 'catalog_entries':'id',
        'recordings':'id', 'youtube_items':'video_id', 'genre_catalog':'key', 'taste_axes':'id', 'rec_weights':'axis_id'}
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
        raise AssertionError('Taste preparation did not finish')
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
            raw = sources.get(owner.source)
            header = owner.capture(conn)
            report['sources'][name] = {'present':owner.source in sources,
                'bytes':len(raw.encode()) if raw is not None else 0,
                'members':header['member_count'] if header is not None else 0}
            assert other.settings.get_setting(owner.source) == raw
        checkpoint = json.loads(sources[preparation.CHECKPOINT])
        checkpoint['weights'].extend({'axis':'controlled-axis:'+str(index), 'weight':.5, 'updated_at':None}
                                    for index in range(8192))
        controls = {
            'checkpoint':checkpoint,
            'baseline':[{'key':'controlled-unresolved:'+str(index), 'rank':index+1, 'score':.5,
                'trace':{'label':'Retained controlled trace', 'position':index}, 'lane':'', 'lane_weight':None}
                for index in range(8192)],
            'decisions':[{'decision':'retire', 'cohort':'controlled', 'reason':'Controlled explicit operator choice',
                'at':2000000000+index, 'powered':False, 'basis':'operator retirement; insufficient live evidence',
                'live_evidence':{'ppr':{'position':index}, 'cosine':None}} for index in range(200)],
        }
        for name, owner in publication.OWNERS.items():
            raw = json.dumps(controls[name])
            store.settings.set_setting(owner.source, raw)
            revision = finish(owner)
            if name == 'baseline':
                with conn:
                    conn.execute('CREATE TRIGGER controlled_taste_interrupt BEFORE INSERT ON '+owner.tables[1]+
                                 " BEGIN SELECT RAISE(ABORT,'controlled publication interruption'); END")
                try:
                    owner.publish(conn, revision=revision)
                except sqlite3.IntegrityError as exc:
                    assert 'controlled publication interruption' in str(exc)
                else:
                    raise AssertionError('Publication interruption was ignored')
                assert not conn.execute('SELECT 1 FROM '+owner.tables[0]+' WHERE revision=?', (revision,)).fetchone()
                with conn:
                    conn.execute('DROP TRIGGER controlled_taste_interrupt')
                def race(connection, header, described):
                    result = publication.verify_files(connection, header, described)
                    with other.conn:
                        other.conn.execute('UPDATE main.settings SET value=value WHERE key=?', (owner.source,))
                    return result
                try:
                    replace(owner, verify_files=race).publish(conn, revision=revision)
                except MigrationError as exc:
                    assert 'changed' in str(exc) or 'reconciled' in str(exc)
                else:
                    raise AssertionError('Stale taste publication was accepted')
                revision = finish(owner)
            publish('controlled-'+name, owner, revision)
            assert other.settings.get_setting(owner.source) == raw
        owner = publication.OWNERS['baseline']
        header = owner.capture(conn)
        Path(header['recovery_filename']).unlink()
        def failed(*args):
            assert not conn.in_transaction
            raise OSError('controlled taste recovery failure')
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
        assert publication.OWNERS['decisions'].selected(conn) is not None
        owner.worker.step(conn, retry=True)
        owner.publish(conn, revision=owner.worker.progress(conn).prepared_revision)
        checkpoint_owner = publication.OWNERS['checkpoint']
        started = time.monotonic()
        store.rec_model.save_taste_settings(checkpoint_owner.source, ['rec_param:breadth_bias'], 2000000500, replace=True)
        report['compatible_checkpoint_save_seconds'] = time.monotonic()-started
        assert checkpoint_owner.selected(other.conn) is None
        assert json.loads(other.settings.get_setting(checkpoint_owner.source))['saved_at'] == 2000000500
        publish('continuing-checkpoint-save', checkpoint_owner, finish(checkpoint_owner))
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
                for key in keys:
                    if key in sources:
                        conn.execute('UPDATE settings SET value=? WHERE key=?', (sources[key], key))
                    else:
                        conn.execute('DELETE FROM settings WHERE key=?', (key,))
            assert before == {owner:fingerprint(conn, owner, order) for owner, order in protected.items()}
            assert history == list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
            report['original_sources_references_weights_history_preserved_after_restore'] = True
        finally:
            report_path.write_text(json.dumps(report, indent=2))
            other.conn.close()
            conn.close()
    print(json.dumps({key:value for key, value in report.items() if key != 'batches'}, indent=2))


if __name__ == '__main__':
    check(Path(sys.argv[1]), Path(sys.argv[2]))
