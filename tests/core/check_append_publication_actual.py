"""Measure operation publication using a disposable genuine v93 library only."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from yt_playlist.core import radio_storage
from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import append_operations as preparation, append_publication as publication
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
        'dataset':'Existing isolated genuine v93 library. Every saved recipe and the actual Radio checkpoint are verified. Controlled operations temporarily update one existing recipe and the actual Radio source using 4096 actual video identities repeated into 8192 ordered slots.',
        'limits':'No native writes, HTTP, providers, runtime workers, remote intents or full historical conversion. Native v94/v97 source formats use focused genuine-predecessor fixtures rather than this v93 actual copy. Original sources restored afterward.',
        'batches':[], 'maps':[], 'publications':{}}
    store, other = Store(path), Store(path)
    conn = store.conn
    names = ('tv_legacy', 'radio_legacy')
    protected = {'settings':'key', 'rec_proposals':'surface', 'identities':'id', 'playlist_scopes':'ref',
        'catalog_entries':'id', 'recordings':'id', 'youtube_items':'video_id', 'rec_recipes':'playlist_ytm'}
    before = {table:fingerprint(conn, table, order) for table, order in protected.items()}
    history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    recipe_row = conn.execute('SELECT playlist_ytm,recipe,created_at FROM main.rec_recipes ORDER BY playlist_ytm LIMIT 1').fetchone()
    radio_row = conn.execute('SELECT value FROM main.settings WHERE key=?', (radio_storage.SESSION_KEY,)).fetchone()
    assert recipe_row is not None and radio_row is not None
    recipe_source, original_recipe, recipe_at = tuple(recipe_row)
    original_radio = radio_row[0]
    sources = {'tv_legacy':recipe_source, 'radio_legacy':radio_storage.SESSION_KEY}
    touched = False
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
        raise AssertionError('Append preparation did not finish')
    def stage(owner):
        for _ in range(1000):
            result = timed('maps', owner, lambda:owner.step(conn, batch_size=32))
            if result.complete:
                return result.id
        raise AssertionError('Append map did not finish')
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
        report['installed_sources'] = {}
        report['installed_operations'] = {}
        for name in names:
            owner = publication.OWNERS[name]
            prepare(owner)
            identity = stage(owner)
            publish('installed', owner, identity)
            report['installed_sources'][name] = owner.stage(conn, identity).copied
            report['installed_operations'][name] = conn.execute('SELECT count(*) FROM '+owner.table('map')+
                ' m JOIN '+owner.header+' g ON g.source=m.source AND g.preparation_revision=m.generation '
                'WHERE m.stage=? AND g.operation_present=1', (identity,)).fetchone()[0]
        assert other.playlist_recipes.get_recipe(recipe_source) == json.loads(original_recipe)
        assert radio_storage.load(other.conn)[0] == json.loads(original_radio)
        videos = [row[0] for row in conn.execute('SELECT video_id FROM youtube_items ORDER BY video_id LIMIT 4096')]
        assert len(videos) == 4096
        ordered = videos*2
        recipe = {**json.loads(original_recipe), 'tv_stream':'controlled-original-stream',
            'tv_refill':{'request_id':'controlled-original-request', 'state':'pending', 'at':0,
                'before':ordered, 'additions':[{'video_id':videos[0], 'paid':1}, {'video_id':videos[0], 'paid':2}], 'items':[]}}
        state = {**json.loads(original_radio), 'phase':'replacing', 'pending':None,
            'replacement':{'allowed':[[], ordered, []], 'confirmed':[videos[0], videos[0]], 'waiting':[],
                'waiting_at':0, 'receipt':{'status':'UNKNOWN', 'videoId':videos[0], 'setVideoId':'captured-handle', 'opaque':[1, None]},
                'adding':{'video_id':videos[0]}, 'proposed':[{'video_id':videos[0], 'paid':1}, {'video_id':videos[0], 'paid':2}],
                'play_prefix':None, 'slot':0, 'choice_id':'controlled-original-choice', 'excluded':[]}}
        touched = True
        store.playlist_recipes.set_recipe(recipe_source, recipe, recipe_at)
        store.settings.set_setting(radio_storage.SESSION_KEY, json.dumps(state, ensure_ascii=False, indent=2))
        report['controlled_members'] = {'tv_before':len(ordered), 'radio_allowed':len(ordered), 'duplicate_metadata_members':4}
        for name in names:
            owner = publication.OWNERS[name]
            assert owner.selected(conn) is None
            prepare(owner)
            identity = stage(owner)
            with conn:
                conn.execute('CREATE TRIGGER controlled_actual_append_cutover BEFORE INSERT ON '+owner.table('head')+
                    " BEGIN SELECT RAISE(ABORT,'controlled cutover interruption'); END")
            try:
                owner.publish(conn, stage=identity)
            except sqlite3.IntegrityError as exc:
                assert 'controlled cutover interruption' in str(exc)
            else:
                raise AssertionError('Append cutover interruption was ignored')
            assert not conn.execute('SELECT 1 FROM '+owner.table('generations')+' WHERE revision=?', (identity,)).fetchone()
            with conn:
                conn.execute('DROP TRIGGER controlled_actual_append_cutover')
            publish('controlled', owner, identity)
        assert other.playlist_recipes.get_recipe(recipe_source) == recipe
        assert radio_storage.load(other.conn)[0] == state
        owner = publication.OWNERS['tv_legacy']
        identity = owner.selected(conn)
        header = owner.capture(conn, identity, recipe_source)
        for field in ('plan_filename', 'recovery_filename'):
            Path(header[field]).unlink()
        Path(conn.execute('SELECT filename FROM '+preparation.table('tv_legacy', 'parts')+
            ' WHERE source=? AND preparation_revision=? LIMIT 1', (recipe_source, header['preparation_revision'])).fetchone()[0]).unlink()
        started = time.monotonic()
        store.init_schema(defer_metadata=True)
        regular.migrate(conn)
        report['installed_ddl_with_missing_file_seconds'] = time.monotonic()-started
        publication.repair('tv_legacy', conn, recipe_source)
        publish('repaired', owner, identity)
        raced = False
        def race(connection, generation):
            nonlocal raced
            publication.verify_document('tv_legacy', connection, generation)
            if not raced:
                raced = True
                with other.conn:
                    other.conn.execute('UPDATE rec_recipes SET created_at=created_at WHERE playlist_ytm=?', (recipe_source,))
        try:
            replace(owner, verify_document=race).publish(conn, stage=identity)
        except MigrationError:
            pass
        else:
            raise AssertionError('Stale append publication was accepted')
        assert owner.selected(conn) is None
        prepare(owner)
        publish('reconciled-race', owner, stage(owner))
        completed = {**recipe, 'tv_refill':{'request_id':'controlled-original-request', 'state':'complete', 'items':[]}}
        started = time.monotonic()
        store.playlist_recipes.set_recipe(recipe_source, completed, recipe_at)
        report['compatible_tv_writer_seconds'] = time.monotonic()-started
        def fail(*args):
            preparation.copy('tv_legacy', *args)
            raise OSError('controlled actual append publication failure')
        try:
            replace(owner.worker, copy=fail).step(conn, batch_size=128)
        except OSError as exc:
            assert 'controlled actual append publication failure' in str(exc)
        else:
            raise AssertionError('Injected append failure did not run')
        failed = owner.worker.progress(conn)
        started = time.monotonic()
        store.init_schema(defer_metadata=True)
        regular.migrate(conn)
        report['installed_ddl_while_failed_seconds'] = time.monotonic()-started
        assert owner.worker.progress(conn) == failed and failed.status == 'failed'
        owner.worker.step(conn, retry=True)
        prepare(owner)
        publish('completed-tv', owner, stage(owner))
        assert other.playlist_recipes.get_recipe(recipe_source) == completed
        radio = publication.OWNERS['radio_legacy']
        ended = {**state, 'replacement':None, 'pending':None, 'phase':'playing'}
        store.settings.set_setting(radio_storage.SESSION_KEY, json.dumps(ended, ensure_ascii=False))
        assert radio.selected(conn) is None
        prepare(radio)
        publish('completed-radio', radio, stage(radio))
        assert radio_storage.load(other.conn)[0] == ended
        assert not conn.execute("SELECT 1 FROM main.sqlite_schema WHERE name IN ('tv_append_intents','radio_operations')").fetchone()
        report.update(status='passed', interrupted_cutover_rolled_back=True, stale_publication_rejected=True,
            exact_files_recovered=True, failed_job_retried_explicitly=True, preopened_operation_readers_verified=True,
            continuing_completion_reconciled=True, no_remote_operation_admitted=True)
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        conn.set_trace_callback(None)
        conn.rollback()
        try:
            if touched:
                with conn:
                    conn.execute('UPDATE main.rec_recipes SET recipe=?,created_at=? WHERE playlist_ytm=?', (original_recipe, recipe_at, recipe_source))
                    conn.execute('UPDATE main.settings SET value=? WHERE key=?', (original_radio, radio_storage.SESSION_KEY))
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
