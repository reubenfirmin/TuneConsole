"""Measure draft publication and continuing edits on an isolated actual library."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import draft_publication as publication, road_trip_drafts as preparation
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
        'dataset':'Existing isolated genuine v93 actual library. Actual draft presence is recorded separately. A controlled draft on an existing actual recipe contains 32768 ordered repeated known/unknown selections, without admitting missing videos or assigning the current account as historical author.',
        'limits':'No native writes, HTTP, providers, runtime workers or full historical conversion. Original draft state restored afterward.',
        'batches':[], 'publications':{}}
    store, other = Store(path), Store(path)
    conn, owner = store.conn, publication.OWNER
    originals = list(map(tuple, conn.execute('SELECT * FROM road_trip_drafts ORDER BY recipe_id')))
    protected = {'settings':'key', 'rec_proposals':'surface', 'identities':'id', 'playlist_scopes':'ref',
        'catalog_entries':'id', 'recordings':'id', 'youtube_items':'video_id',
        'road_trip_recipes':'id', 'road_trip_drafts':'recipe_id'}
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
                return owner.step(conn, batch_size=128).id
        raise AssertionError('Draft preparation did not finish')
    def publish(name, identity):
        reservations.clear()
        started = time.monotonic()
        owner.publish(conn, stage=identity)
        ended = time.monotonic()
        assert owner.selected(other.conn) == identity
        report['publications'][name] = {'stage':identity, 'seconds':ended-started,
            'writer_seconds':ended-reservations[-1]}
    recipe = None
    try:
        store.init_schema(defer_metadata=True)
        other.init_schema(defer_metadata=True)
        conn.set_trace_callback(trace)
        owner.worker.install(conn)
        publish('installed', ready())
        report['installed_drafts'] = len(originals)
        recipe = conn.execute('SELECT id FROM road_trip_recipes ORDER BY id LIMIT 1').fetchone()[0]
        known = conn.execute("SELECT video_id FROM catalog_entries WHERE video_id IS NOT NULL ORDER BY id LIMIT 1").fetchone()[0]
        value = {'revision':'controlled-held', 'picks':[known]*16384, 'banned':['controlled-unadmitted']*16384,
            'building':True, 'candidate':{'original':['repeat','repeat',None], 'label':'東京'}}
        raw = json.dumps(value, ensure_ascii=False, indent=2)
        report['controlled_original_bytes'] = len(raw.encode())
        with conn:
            conn.execute('INSERT INTO road_trip_drafts VALUES (?,?,?) ON CONFLICT(recipe_id) DO UPDATE '
                'SET state=excluded.state,updated_at=excluded.updated_at', (recipe, raw, 2000000000))
        selected = ready()
        owner.install(conn)
        with conn:
            conn.execute('CREATE TRIGGER controlled_actual_draft_cutover BEFORE INSERT ON '+owner.table('head')+
                " BEGIN SELECT RAISE(ABORT,'controlled draft cutover interruption'); END")
        try:
            owner.publish(conn, stage=selected)
        except sqlite3.IntegrityError as exc:
            assert 'controlled draft cutover interruption' in str(exc)
        else:
            raise AssertionError('Interrupted draft cutover was accepted')
        assert not conn.execute('SELECT 1 FROM '+owner.table('generations')+' WHERE revision=?', (selected,)).fetchone()
        with conn:
            conn.execute('DROP TRIGGER controlled_actual_draft_cutover')
        publish('controlled-32768', selected)
        assert owner.value(other.conn, selected, recipe) == raw
        assert other.road_trip.get_road_trip_draft(recipe) == value
        assert other.road_trip.latest_road_trip_draft() == {'recipe_id':recipe, 'state':value}
        header = owner.capture(conn, selected, recipe)
        assert header['account_id'] is None and header['legacy_author_unknown'] == 1
        filename = conn.execute('SELECT filename FROM '+preparation.PARTS+
            ' WHERE recipe_id=? AND generation=? ORDER BY position LIMIT 1', (recipe, header['generation'])).fetchone()[0]
        Path(filename).unlink()
        started = time.monotonic()
        store.init_schema(defer_metadata=True)
        regular.migrate(conn)
        report['installed_ddl_with_missing_file_seconds'] = time.monotonic()-started
        publication.repair(conn, recipe)
        publish('repaired-32768', selected)
        def race(connection, header):
            publication.verify_document(connection, header)
            with other.conn:
                other.conn.execute('UPDATE road_trip_drafts SET updated_at=updated_at WHERE recipe_id=?', (recipe,))
        try:
            replace(owner, verify_document=race).publish(conn, stage=selected)
        except MigrationError:
            pass
        else:
            raise AssertionError('Stale draft cutover was accepted')
        selected = ready()
        publish('reconciled-after-race', selected)
        changed = {**value, 'revision':'controlled-next', 'building':False}
        started = time.monotonic()
        assert store.road_trip.replace_road_trip_draft(recipe, changed, 2000000001, expected_revision='controlled-held')
        report['compatible_conditional_edit_seconds'] = time.monotonic()-started
        assert owner.selected(other.conn) is None
        assert not store.road_trip.replace_road_trip_draft(recipe, {'revision':'stale'}, 2000000002, expected_revision='controlled-held')
        def fail(*args):
            assert not conn.in_transaction
            raise OSError('controlled draft preparation failure')
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
        assert other.road_trip.get_road_trip_draft(recipe) == changed
        assert not conn.execute("SELECT 1 FROM catalog_entries WHERE video_id='controlled-unadmitted'").fetchone()
        regular.validate(conn)
        report.update(status='passed', interrupted_cutover_rolled_back=True, stale_cutover_rejected=True,
            exact_files_recovered=True, conditional_writer_rejected_stale_revision=True,
            failed_preparation_retried_explicitly=True, preopened_readers_use_publications=True)
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        conn.set_trace_callback(None)
        conn.rollback()
        try:
            with conn:
                if recipe is not None:
                    original = next((row for row in originals if row[0] == recipe), None)
                    if original is None:
                        conn.execute('DELETE FROM road_trip_drafts WHERE recipe_id=?', (recipe,))
                    else:
                        conn.execute('INSERT INTO road_trip_drafts VALUES (?,?,?) ON CONFLICT(recipe_id) DO UPDATE '
                            'SET state=excluded.state,updated_at=excluded.updated_at', original)
            assert before == {table:fingerprint(conn, table, order) for table, order in protected.items()}
            assert history == list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
            report['sources_references_history_preserved_after_restore'] = True
        finally:
            report_path.write_text(json.dumps(report, indent=2))
            other.conn.close()
            conn.close()
    print(json.dumps({key:value for key, value in report.items() if key != 'batches'}, indent=2))


if __name__ == '__main__':
    check(Path(sys.argv[1]), Path(sys.argv[2]))
