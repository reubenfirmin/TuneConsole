"""Measure retained clicked-pool publication on an isolated actual library."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from yt_playlist.core import library_pool_state
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import clicked_publication as publication, ranked_state as preparation
from yt_playlist.core.store import Store


def fingerprint(conn, table, order):
    digest, count = hashlib.sha256(), 0
    for row in conn.execute('SELECT * FROM main.'+table+' ORDER BY '+order):
        digest.update(json.dumps(tuple(row), ensure_ascii=False).encode())
        count += 1
    return count, digest.hexdigest()


def check(path, report_path):
    assert path.parent == report_path.parent == Path('/tmp') and path.name.startswith('tuneconsole-')
    report = {'status':'failed', 'database':str(path), 'python':sys.version, 'sqlite':sqlite3.sqlite_version,
        'dataset':'Existing isolated genuine v93 library. Installed clicked-pool presence is recorded separately. Four controlled lanes each use 8192 existing recording identities and one existing recorded policy; unknown genre references remain unresolved.',
        'limits':'No native writes, HTTP, providers, runtime workers or full historical conversion. Exact original proposals restored afterward.',
        'batches':[], 'maps':[], 'publications':{}}
    store, other = Store(path), Store(path)
    conn, owner = store.conn, publication.OWNER
    originals = {surface:conn.execute('SELECT payload,built_at FROM rec_proposals WHERE surface=?', (surface,)).fetchone()
        for surface in library_pool_state.SURFACES}
    originals = {surface:tuple(row) if row else None for surface, row in originals.items()}
    protected = {'settings':'key', 'rec_proposals':'surface', 'identities':'id', 'playlist_scopes':'ref',
        'catalog_entries':'id', 'recordings':'id', 'youtube_items':'video_id', 'recommendation_policies':'version'}
    before = {table:fingerprint(conn, table, order) for table, order in protected.items()}
    history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    reservations = []
    def trace(sql):
        if sql == 'BEGIN IMMEDIATE':
            reservations.append(time.monotonic())
    def ready():
        for _ in range(2000):
            reservations.clear()
            started = time.monotonic()
            progress = owner.worker.step(conn, batch_size=128)
            ended = time.monotonic()
            report['batches'].append({'seconds':ended-started,
                'writer_seconds':ended-reservations[-1] if reservations else 0})
            if progress.status == 'prepared':
                break
        else:
            raise AssertionError('Clicked preparation did not finish')
        for _ in range(100):
            reservations.clear()
            started = time.monotonic()
            stage = owner.step(conn, batch_size=2)
            ended = time.monotonic()
            report['maps'].append({'seconds':ended-started,
                'writer_seconds':ended-reservations[-1] if reservations else 0, 'copied':stage.copied})
            if stage.complete:
                return stage.id
        raise AssertionError('Clicked map did not finish')
    def publish(name, stage):
        reservations.clear()
        started = time.monotonic()
        owner.publish(conn, stage=stage)
        ended = time.monotonic()
        assert owner.selected(other.conn) == stage
        report['publications'][name] = {'stage':stage, 'seconds':ended-started,
            'writer_seconds':ended-reservations[-1]}
    try:
        store.init_schema(defer_metadata=True)
        other.init_schema(defer_metadata=True)
        conn.set_trace_callback(trace)
        owner.worker.install(conn)
        report['installed'] = {surface:{'present':row is not None,
            'bytes':len(row[0].encode()) if row and row[0] else 0} for surface, row in originals.items()}
        publish('installed', ready())
        for surface, row in originals.items():
            assert other.rec_proposals._proposal_json(surface) == (row[0] if row else None)
        keys = [row[0] for row in conn.execute('SELECT identity_key FROM recordings ORDER BY id LIMIT 8192')]
        assert len(keys) == 8192
        policy = conn.execute('SELECT version FROM recommendation_policies ORDER BY created_at DESC,version LIMIT 1').fetchone()[0]
        tracks = [{'key':key, 'score':1/(position+1), 'pool_policy':policy, 'reason':'Controlled actual recording'}
            for position, key in enumerate(keys)]
        values = {surface:{'parameters':{'lane':surface.split(':')[1], 'epoch':0,
            'include_genres':['other:controlled-unregistered'], 'avoid_genres':[]}, 'tracks':tracks}
            for surface in library_pool_state.SURFACES}
        raw = {surface:json.dumps(value, ensure_ascii=False, indent=2) for surface, value in values.items()}
        report['controlled_bytes'] = {surface:len(value.encode()) for surface, value in raw.items()}
        with conn:
            conn.executemany('INSERT INTO rec_proposals VALUES (?,?,NULL) ON CONFLICT(surface) DO UPDATE '
                'SET payload=excluded.payload,built_at=excluded.built_at', list(raw.items()))
        stage = ready()
        with conn:
            conn.execute('CREATE TRIGGER controlled_actual_clicked_cutover BEFORE INSERT ON '+owner.table('head')+
                " BEGIN SELECT RAISE(ABORT,'controlled clicked cutover interruption'); END")
        try:
            owner.publish(conn, stage=stage)
        except sqlite3.IntegrityError as exc:
            assert 'controlled clicked cutover interruption' in str(exc)
        else:
            raise AssertionError('Interrupted clicked cutover was accepted')
        assert not conn.execute('SELECT 1 FROM '+owner.table('generations')+' WHERE revision=?', (stage,)).fetchone()
        with conn:
            conn.execute('DROP TRIGGER controlled_actual_clicked_cutover')
        publish('four-lanes-32768-candidates', stage)
        for surface in library_pool_state.SURFACES:
            assert owner.value(other.conn, stage, surface) == raw[surface]
            assert other.rec_proposals.get_proposals(surface) == values[surface]
            header = owner.capture(conn, stage, surface)
            assert header['built_at'] is None
            assert conn.execute('SELECT count(*) FROM prepared_clicked_pools_candidates WHERE surface=? AND preparation_revision=?',
                (surface, header['preparation_revision'])).fetchone()[0] == 8192
        surface = 'home_pool:wheelhouse'
        filename = conn.execute('SELECT filename FROM prepared_clicked_pools_outputs WHERE surface=? AND preparation_revision=? LIMIT 1',
            (surface, owner.capture(conn, stage, surface)['preparation_revision'])).fetchone()[0]
        Path(filename).unlink()
        started = time.monotonic()
        store.init_schema(defer_metadata=True)
        regular.migrate(conn)
        report['installed_ddl_with_missing_file_seconds'] = time.monotonic()-started
        publication.repair(conn, surface)
        publish('repaired-output', stage)
        def race(connection, header):
            publication.verify_document(connection, header)
            with other.conn:
                other.conn.execute('UPDATE rec_proposals SET built_at=built_at WHERE surface=?', (header['surface'],))
        try:
            replace(owner, verify_document=race).publish(conn, stage=stage)
        except MigrationError:
            pass
        else:
            raise AssertionError('Stale clicked publication was accepted')
        publish('reconciled-source-race', ready())
        changed = {'parameters':{**values[surface]['parameters'], 'epoch':1}, 'tracks':tracks[:32]}
        started = time.monotonic()
        store.rec_proposals.put_proposals(surface, changed, 2000000000)
        report['compatible_writer_seconds'] = time.monotonic()-started
        assert owner.selected(other.conn) is None
        assert other.rec_proposals.get_proposals(surface) == changed
        def fail(*args, **kwargs):
            assert not conn.in_transaction
            raise OSError('controlled failed clicked preparation')
        try:
            replace(owner.worker, prepare=fail).step(conn)
        except OSError:
            pass
        failed = owner.worker.progress(conn)
        assert failed.status == 'failed'
        started = time.monotonic()
        store.init_schema(defer_metadata=True)
        regular.migrate(conn)
        report['installed_ddl_while_failed_seconds'] = time.monotonic()-started
        assert owner.worker.progress(conn) == failed
        owner.worker.step(conn, retry=True)
        publish('continuing-edit', ready())
        assert other.rec_proposals.get_proposals(surface) == changed
        regular.validate(conn)
        report.update(status='passed', interrupted_cutover_rolled_back=True, stale_cutover_rejected=True,
            exact_files_recovered=True, failed_preparation_retried_explicitly=True, preopened_readers_use_publications=True)
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        conn.set_trace_callback(None)
        conn.rollback()
        try:
            with conn:
                for surface, row in originals.items():
                    if row is None:
                        conn.execute('DELETE FROM rec_proposals WHERE surface=?', (surface,))
                    else:
                        conn.execute('INSERT INTO rec_proposals VALUES (?,?,?) ON CONFLICT(surface) DO UPDATE '
                            'SET payload=excluded.payload,built_at=excluded.built_at', (surface, *row))
            assert before == {table:fingerprint(conn, table, order) for table, order in protected.items()}
            assert history == list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
            report['sources_references_history_preserved_after_restore'] = True
        finally:
            report_path.write_text(json.dumps(report, indent=2))
            other.conn.close()
            conn.close()
    print(json.dumps({key:value for key, value in report.items() if key not in ('batches', 'maps')}, indent=2))


if __name__ == '__main__':
    check(Path(sys.argv[1]), Path(sys.argv[2]))
