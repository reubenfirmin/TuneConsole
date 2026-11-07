"""Measure TV cutovers and continuing writers on an isolated actual library."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import artist_lookup, tv_publication as publication, tv_state as preparation
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
        'dataset':'Existing isolated genuine v93 actual library. Installed TV state presence is recorded separately. Controlled documents fill the five native TV budgets, including 60 receipts with 60 ordered repeated-across-receipts items each. References use existing library artists and videos without admission.',
        'limits':'No native writes, HTTP, providers, runtime workers or full historical conversion. Original proposals restored afterward.',
        'batches':[], 'publications':{}, 'compatible_writers':{}}
    store, other = Store(path), Store(path)
    conn = store.conn
    originals = {source:conn.execute('SELECT payload,built_at FROM rec_proposals WHERE surface=?', (source,)).fetchone()
                 for source in publication.SOURCES}
    originals = {source:tuple(row) if row else None for source, row in originals.items()}
    protected = {'settings':'key', 'rec_proposals':'surface', 'identities':'id', 'playlist_scopes':'ref',
        'catalog_entries':'id', 'recordings':'id', 'youtube_items':'video_id', 'artists':'id', 'artist_aliases':'artist_id,alias'}
    before = {table:fingerprint(conn, table, order) for table, order in protected.items()}
    history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    reservations = []
    def trace(sql):
        if sql == 'BEGIN IMMEDIATE':
            reservations.append(time.monotonic())
    def finish(worker):
        for _ in range(2000):
            reservations.clear()
            started = time.monotonic()
            progress = worker.step(conn, batch_size=128)
            ended = time.monotonic()
            report['batches'].append({'seconds':ended-started,
                'writer_seconds':ended-reservations[-1] if reservations else 0})
            if progress.status == 'prepared':
                return progress.prepared_revision
        raise AssertionError('TV preparation did not finish its bounded jobs')
    def publish(name, owner, revision):
        reservations.clear()
        started = time.monotonic()
        owner.publish(conn, revision=revision)
        ended = time.monotonic()
        assert owner.selected(other.conn) == revision
        report['publications'][name+':'+owner.source] = {'revision':revision, 'seconds':ended-started,
            'writer_seconds':ended-reservations[-1]}
    def write(name, function):
        started = time.monotonic()
        function()
        report['compatible_writers'][name] = time.monotonic()-started
    try:
        store.init_schema(defer_metadata=True)
        other.init_schema(defer_metadata=True)
        conn.set_trace_callback(trace)
        artist_lookup.JOB.install(conn)
        finish(artist_lookup.JOB)
        report['installed'] = {source:{'present':row is not None,
            'bytes':len(row[0].encode()) if row and row[0] is not None else 0} for source, row in originals.items()}
        for owner in publication.OWNERS.values():
            owner.worker.install(conn)
            publish('installed', owner, finish(owner.worker))
            assert other.rec_proposals._proposal_json(owner.source) == (originals[owner.source][0] if originals[owner.source] else None)
        artist = conn.execute('SELECT alias FROM artist_aliases ORDER BY artist_id,alias LIMIT 1').fetchone()[0]
        known = [tuple(row) for row in conn.execute('SELECT y.video_id,r.identity_key FROM youtube_items y '
            'JOIN recordings r ON r.id=y.recording_id ORDER BY y.video_id LIMIT 60')]
        assert len(known) == 60
        items = [{'video_id':video, 'key':video, 'title':'Controlled actual video '+str(index),
            'source_key':key, 'artist':artist, 'group':'liked', 'duration':100} for index, (video, key) in enumerate(known)]
        values = {'tv_pool':{'schema':3, 'key':'controlled', 'at':9007199254740993, 'items':items,
                'attempted':list(dict.fromkeys(key for _, key in known)), 'state':'searching'},
            'tv_artist_artwork':{artist if index == 0 else 'controlled-unknown-'+str(index):
                {'images':['https://example.test/'+str(index)+'/'+str(position) for position in range(6)],
                 'at':None, 'retry_at':9007199254740993} for index in range(60)},
            'tv_artist_artwork_requests':{'controlled-'+str(index):artist if index == 0 else 'Unknown '+str(index) for index in range(60)},
            'tv_visual_checks':{f'visual{index:05d}':{'verdict':'static' if index % 2 else 'moving', 'version':2,
                'at':9007199254740993, 'difference':0, 'changed_fraction':0, 'sample_times':[20,60]} for index in range(240)},
            'tv_continuations':{f'{1000000+index}::controlled:request:{index}':{'playlist_id':2000000+index,
                'stream':'controlled-next', 'items':items} for index in range(60)}}
        raw = {source:json.dumps(value, ensure_ascii=False, indent=2) for source, value in values.items()}
        report['controlled_bytes'] = {source:len(value.encode()) for source, value in raw.items()}
        with conn:
            conn.executemany('INSERT INTO rec_proposals VALUES (?,?,?) ON CONFLICT(surface) DO UPDATE SET '
                'payload=excluded.payload,built_at=excluded.built_at', [(source, value, 9007199254740993) for source, value in raw.items()])
        for owner in publication.OWNERS.values():
            revision = finish(owner.worker)
            if owner.source == 'tv_continuations':
                owner.install(conn)
                with conn:
                    conn.execute('CREATE TRIGGER controlled_tv_actual_interrupt BEFORE INSERT ON '+owner.tables[1]+
                        " BEGIN SELECT RAISE(ABORT,'controlled TV interruption'); END")
                try:
                    owner.publish(conn, revision=revision)
                except sqlite3.IntegrityError as exc:
                    assert 'controlled TV interruption' in str(exc)
                else:
                    raise AssertionError('Interrupted TV cutover was accepted')
                assert not conn.execute('SELECT 1 FROM '+owner.tables[0]+' WHERE revision=?', (revision,)).fetchone()
                with conn:
                    conn.execute('DROP TRIGGER controlled_tv_actual_interrupt')
                def race(connection, header, value):
                    publication.verify_rows(connection, header, value)
                    with other.conn:
                        other.conn.execute('UPDATE rec_proposals SET built_at=built_at WHERE surface=?', (owner.source,))
                try:
                    replace(owner, verify_rows=race).publish(conn, revision=revision)
                except MigrationError as exc:
                    assert 'changed' in str(exc) or 'reconciled' in str(exc)
                else:
                    raise AssertionError('Stale TV cutover was accepted')
                revision = finish(owner.worker)
            publish('controlled-budget', owner, revision)
            assert other.rec_proposals._proposal_json(owner.source) == raw[owner.source]
            assert other.rec_proposals.get_proposals(owner.source) == values[owner.source]
        pool = publication.OWNERS['pool']
        filename = conn.execute('SELECT filename FROM '+preparation.tables(pool.source)[2]+
            ' WHERE preparation_revision=? LIMIT 1', (pool.capture(conn)['preparation_revision'],)).fetchone()[0]
        Path(filename).unlink()
        def fail(*args):
            assert not conn.in_transaction
            raise OSError('controlled TV recovery failure')
        try:
            replace(pool.worker, repair_files=fail).step(conn, retry=True)
        except OSError:
            pass
        failed = pool.worker.progress(conn)
        assert failed.status == 'failed'
        started = time.monotonic()
        store.init_schema(defer_metadata=True)
        regular.migrate(conn)
        report['installed_ddl_while_failed_seconds'] = time.monotonic()-started
        assert pool.worker.progress(conn) == failed
        pool.worker.step(conn, retry=True)
        publish('recovered', pool, pool.worker.progress(conn).prepared_revision)
        write('request-artwork', lambda:store.tv.request_artwork('controlled-new', artist, 99))
        write('publish-artwork-and-complete-request', lambda:store.tv.publish_artwork('controlled-new',
            {'images':['https://example.test/new'], 'at':99}, 99, (None, None), complete=True))
        write('publish-visual', lambda:store.tv.publish_visual('visual00240', {'verdict':'moving', 'version':2}, 99))
        write('complete-receipt', lambda:store.tv.complete_receipt('3000000:s:r',
            {'playlist_id':3000001, 'stream':'real-writer', 'items':items[:1]}, 99))
        write('replace-pool', lambda:store.rec_proposals.put_proposals('tv_pool', {'items':items[:1], 'at':99}, 99))
        for owner in publication.OWNERS.values():
            assert owner.selected(other.conn) is None
            publish('continuing-writes', owner, finish(owner.worker))
        assert 'controlled-new' not in other.rec_proposals.get_proposals('tv_artist_artwork_requests')
        assert other.tv.receipt('3000000:s:r')['stream'] == 'real-writer'
        assert 'visual00240' not in other.tv.static_ids(2)
        regular.validate(conn)
        report.update(status='passed', interrupted_cutover_rolled_back=True, stale_cutover_rejected=True,
            explicit_file_recovery=True, preopened_readers_use_publications=True)
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        conn.set_trace_callback(None)
        conn.rollback()
        try:
            with conn:
                for source, original in originals.items():
                    if original is not None:
                        conn.execute('INSERT INTO rec_proposals VALUES (?,?,?) ON CONFLICT(surface) DO UPDATE SET '
                            'payload=excluded.payload,built_at=excluded.built_at', (source, *original))
                    else:
                        conn.execute('DELETE FROM rec_proposals WHERE surface=?', (source,))
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
