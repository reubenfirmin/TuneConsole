"""Measure saved-document map staging and cutover on an isolated actual library."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import collection_documents as preparation, collection_document_publication as publication
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
        'dataset':'Existing isolated genuine v93 actual library, including every saved recipe and canvas. Controlled continuing updates replace one existing recipe with 8192 repeated genres and two eras, and one canvas with 512 KiB of verbatim text.',
        'limits':'No native writes, HTTP, providers, runtime workers or full historical conversion. Original recipes and canvases restored afterward.',
        'preparation_batches':[], 'map_batches':[], 'publications':{}}
    store, other = Store(path), Store(path)
    conn = store.conn
    originals = {name:list(map(tuple, conn.execute('SELECT * FROM main.'+preparation.SOURCES[name][0]+' ORDER BY playlist_ytm')))
        for name in publication.OWNERS}
    protected = {'settings':'key', 'rec_proposals':'surface', 'identities':'id', 'playlist_scopes':'ref',
        'catalog_entries':'id', 'recordings':'id', 'youtube_items':'video_id', 'taste_axes':'id',
        'rec_recipes':'playlist_ytm', 'cluster_canvas':'playlist_ytm'}
    before = {table:fingerprint(conn, table, order) for table, order in protected.items()}
    history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    reservations = []
    def trace(sql):
        if sql == 'BEGIN IMMEDIATE':
            reservations.append(time.monotonic())
    def timed(category, function):
        reservations.clear()
        started = time.monotonic()
        result = function()
        ended = time.monotonic()
        report[category].append({'seconds':ended-started,
            'writer_seconds':ended-reservations[-1] if reservations else 0})
        return result
    def prepare(owner):
        owner.worker.install(conn)
        for _ in range(1000):
            progress = timed('preparation_batches', lambda:owner.worker.step(conn, batch_size=128))
            if progress.status == 'prepared':
                return
        raise AssertionError('Saved-document preparation did not finish')
    def stage(owner):
        for _ in range(1000):
            result = timed('map_batches', lambda:owner.step(conn, batch_size=32))
            if result.complete:
                return result.id
        raise AssertionError('Saved-document map did not finish')
    def publish(name, owner, identity):
        reservations.clear()
        started = time.monotonic()
        owner.publish(conn, stage=identity)
        ended = time.monotonic()
        assert owner.selected(other.conn) == identity
        report['publications'][name+':'+owner.name] = {'stage':identity, 'seconds':ended-started,
            'writer_seconds':ended-reservations[-1], 'documents':owner.stage(conn, identity).copied}
    try:
        store.init_schema(defer_metadata=True)
        other.init_schema(defer_metadata=True)
        conn.set_trace_callback(trace)
        report['installed_counts'] = {name:len(rows) for name, rows in originals.items()}
        report['installed_bytes'] = {name:sum(len(row[1].encode()) if row[1] is not None else 0 for row in rows)
            for name, rows in originals.items()}
        for name, owner in publication.OWNERS.items():
            prepare(owner)
            identity = stage(owner)
            publish('installed', owner, identity)
            for source, raw, _ in originals[name]:
                assert owner.value(other.conn, identity, source) == raw
            if name == 'recipes':
                assert other.playlist_recipes.get_recipe_created_ats() == {row[0]:row[2] for row in originals[name]}
        recipe_source, _, _ = originals['recipes'][0]
        canvas_source, _, _ = originals['canvases'][0]
        recipe = {'media':'video', 'facets':{'genres':['Controlled unknown genre']*8192, 'eras':['2010','2010']},
            'opaque':{'ordered':[2,2,1], 'label':'東京'}}
        canvas = 'Controlled verbatim canvas\n'+('x'*512*1024)
        store.playlist_recipes.set_recipe(recipe_source, recipe, 2000000000)
        store.playlist_recipes.set_cluster_canvas(canvas_source, canvas, None)
        report['controlled_bytes'] = {'recipe':len(json.dumps(recipe).encode()), 'canvas':len(canvas.encode())}
        for name, owner in publication.OWNERS.items():
            assert owner.selected(other.conn) is None
            prepare(owner)
            owner.install(conn)
            before_stage = conn.execute('SELECT max(id) FROM '+owner.table('stages')).fetchone()[0]
            with conn:
                conn.execute('CREATE TRIGGER controlled_actual_map_interrupt BEFORE INSERT ON '+owner.table('map')+
                    " BEGIN SELECT RAISE(ABORT,'controlled map interruption'); END")
            try:
                owner.step(conn, batch_size=32)
            except sqlite3.IntegrityError as exc:
                assert 'controlled map interruption' in str(exc)
            else:
                raise AssertionError('Map interruption was ignored')
            assert conn.execute('SELECT max(id) FROM '+owner.table('stages')).fetchone()[0] == before_stage
            with conn:
                conn.execute('DROP TRIGGER controlled_actual_map_interrupt')
            identity = stage(owner)
            with conn:
                conn.execute('CREATE TRIGGER controlled_actual_document_cutover BEFORE INSERT ON '+owner.table('head')+
                    " BEGIN SELECT RAISE(ABORT,'controlled cutover interruption'); END")
            try:
                owner.publish(conn, stage=identity)
            except sqlite3.IntegrityError as exc:
                assert 'controlled cutover interruption' in str(exc)
            else:
                raise AssertionError('Cutover interruption was ignored')
            assert not conn.execute('SELECT 1 FROM '+owner.table('generations')+' WHERE revision=?', (identity,)).fetchone()
            with conn:
                conn.execute('DROP TRIGGER controlled_actual_document_cutover')
            publish('controlled-continuing-update', owner, identity)
        assert other.playlist_recipes.get_recipe(recipe_source) == recipe
        assert other.playlist_recipes.get_cluster_canvas(canvas_source) == canvas
        owner = publication.OWNERS['recipes']
        header = owner.capture(conn, owner.selected(conn), recipe_source)
        Path(header['recovery_filename']).unlink()
        started = time.monotonic()
        store.init_schema(defer_metadata=True)
        regular.migrate(conn)
        report['installed_ddl_with_missing_retained_file_seconds'] = time.monotonic()-started
        publication.repair('recipes', conn, recipe_source)
        publish('repaired', owner, owner.selected(conn))
        real_verify = owner.verify_document
        raced = False
        def race(connection, header):
            nonlocal raced
            real_verify(connection, header)
            if not raced:
                raced = True
                with other.conn:
                    other.conn.execute('UPDATE rec_recipes SET created_at=created_at WHERE playlist_ytm=?', (recipe_source,))
        try:
            replace(owner, verify_document=race).publish(conn, stage=owner.selected(conn))
        except MigrationError:
            pass
        else:
            raise AssertionError('Stale saved-document map was accepted')
        assert owner.selected(conn) is None
        prepare(owner)
        publish('reconciled-after-race', owner, stage(owner))
        regular.validate(conn)
        report.update(status='passed', interrupted_map_rolled_back=True, interrupted_cutover_rolled_back=True,
            stale_map_rejected=True, exact_files_recovered=True, preopened_readers_use_publications=True)
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        conn.set_trace_callback(None)
        conn.rollback()
        try:
            with conn:
                for name, rows in originals.items():
                    owner, key, field = preparation.SOURCES[name]
                    conn.executemany('UPDATE '+owner+' SET '+field+'=?,created_at=? WHERE '+key+'=?',
                        [(raw, at, source) for source, raw, at in rows])
            assert before == {table:fingerprint(conn, table, order) for table, order in protected.items()}
            assert history == list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
            report['sources_references_history_preserved_after_restore'] = True
        finally:
            report_path.write_text(json.dumps(report, indent=2))
            other.conn.close()
            conn.close()
    print(json.dumps({key:value for key, value in report.items() if key not in ('preparation_batches','map_batches')}, indent=2))


if __name__ == '__main__':
    check(Path(sys.argv[1]), Path(sys.argv[2]))
