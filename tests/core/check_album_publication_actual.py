"""Measure occurrence cutover on a disposable genuine v93 library only."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from yt_playlist.core.migrations import MigrationError, regular
from yt_playlist.core.preparation import album_occurrences as preparation, album_publication as publication
from yt_playlist.core.store import Store


def fingerprint(conn, owner, order):
    digest, count = hashlib.sha256(), 0
    for row in conn.execute('SELECT * FROM main.'+owner+' ORDER BY '+order):
        digest.update(json.dumps(tuple(row), ensure_ascii=False).encode())
        count += 1
    return count, digest.hexdigest()


def expected_projection(conn):
    """Derive the publication contract directly from installed source evidence."""
    rows = [dict(row) for row in conn.execute('SELECT * FROM main.album_tracks ORDER BY id')]
    states = {row['album_id']:dict(row) for row in conn.execute('SELECT * FROM main.album_fetch_state')}
    full = {}
    for row in rows:
        if row['is_full']:
            full.setdefault(row['album_id'], []).append(row)
    complete = {album for album, members in full.items() if album in states and states[album]['complete'] and
        members and states[album]['expected_track_count'] == len(members)}
    full_entries = {(row['album_id'],row['entry_id']) for row in rows if row['is_full'] and row['album_id'] in complete and row['entry_id'] is not None}
    visible = [row for row in rows if (row['is_full'] and row['album_id'] in complete) or
        (not row['is_full'] and (row['album_id'],row['entry_id']) not in full_entries)]
    groups = {}
    for album in sorted(full.keys() | states.keys()):
        members, state = full.get(album, []), states.get(album)
        known = sum(row['position'] is not None for row in members)
        provider = conn.execute("SELECT provider_id FROM main.album_provider_ids WHERE album_id=? AND provider='youtube' ORDER BY is_primary DESC,provider_id LIMIT 1", (album,)).fetchone()
        assert provider is not None
        groups[album] = (provider[0],len(members),'known' if known == len(members) else 'unknown' if not known else 'mixed',
            'complete' if album in complete else 'partial',state['expected_track_count'] if state else None,state['fetched_at'] if state else None)
    return rows, states, visible, groups


def check(path, report_path):
    assert path.parent == report_path.parent == Path('/tmp') and path.name.startswith('tuneconsole-')
    report = {'status':'failed', 'database':str(path), 'python':sys.version, 'sqlite':sqlite3.sqlite_version,
        'dataset':'Existing isolated genuine v93 library, every installed album row/fetch state. A controlled 8192-occurrence capture temporarily uses 4096 actual catalog entries twice on an existing release without a full capture or fetch state.',
        'limits':'No native writes, HTTP, providers, runtime workers or full historical conversion. Native destination equivalence uses genuine focused predecessor fixtures. The actual-copy expected projection is independently derived from original full/incidental rows and recorded fetch counts. Sources restored afterward.',
        'batches':[], 'copies':[], 'publications':{}}
    store, other = Store(path), Store(path)
    conn = store.conn
    protected = {'album_tracks':'id', 'album_fetch_state':'album_id', 'album_share_snapshots':'browse_id',
        'albums':'id', 'album_provider_ids':'provider,provider_id', 'settings':'key', 'identities':'id',
        'playlist_scopes':'ref', 'catalog_entries':'id', 'recordings':'id', 'youtube_items':'video_id'}
    before = {table:fingerprint(conn, table, order) for table, order in protected.items()}
    history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    controlled_album, source = tuple(conn.execute("SELECT p.album_id,p.provider_id FROM main.album_provider_ids p WHERE p.provider='youtube' AND p.is_primary=1 "
        'AND NOT EXISTS(SELECT 1 FROM main.album_tracks t WHERE t.album_id=p.album_id AND t.is_full=1) '
        'AND NOT EXISTS(SELECT 1 FROM main.album_fetch_state f WHERE f.album_id=p.album_id) ORDER BY p.album_id LIMIT 1').fetchone())
    added, touched, reservations = [], False, []
    def trace(sql):
        if sql == 'BEGIN IMMEDIATE':
            reservations.append(time.monotonic())
    def timed(category, function):
        reservations.clear()
        started = time.monotonic()
        result = function()
        ended = time.monotonic()
        report[category].append({'seconds':ended-started, 'writer_seconds':ended-reservations[-1] if reservations else 0})
        return result
    def prepare():
        preparation.JOB.install(conn)
        for _ in range(3000):
            if timed('batches', lambda:preparation.JOB.step(conn, batch_size=32)).status == 'prepared':
                return
        raise AssertionError('Album occurrence preparation did not finish')
    def stage():
        for _ in range(3000):
            current = timed('copies', lambda:publication.step(conn, batch_size=32))
            if current.complete:
                return current.id
        raise AssertionError('Album occurrence custody did not finish')
    def publish(label, identity):
        reservations.clear()
        started = time.monotonic()
        publication.publish(conn, identity=identity)
        ended = time.monotonic()
        assert publication.selected(other.conn) == identity
        report['publications'][label] = {'stage':identity,'seconds':ended-started,'writer_seconds':ended-reservations[-1]}
    def compare(identity):
        rows, states, visible, groups = expected_projection(conn)
        relation = publication.relation(other.conn, 'album_tracks')
        assert [dict(row) for row in other.conn.execute('SELECT * FROM '+relation+' ORDER BY id')] == visible
        assert list(map(tuple, other.conn.execute('SELECT * FROM '+publication.relation(other.conn,'album_fetch_state')+' ORDER BY album_id'))) == [
            (album,values[-1],1,values[-2]) for album,values in groups.items() if values[3] == 'complete']
        assert {row['album_id']:tuple(row)[2:] for row in conn.execute('SELECT * FROM '+publication.table('groups')+' WHERE stage=?', (identity,))} == groups
        for kind, key, originals in [('tracks','row_id',{row['id']:row for row in rows}), ('states','album_id',states)]:
            captured = list(conn.execute('SELECT * FROM '+publication.table(kind)+' WHERE stage=?', (identity,)))
            assert len(captured) == len(originals)
            for row in captured:
                owner = 'album_tracks' if kind == 'tracks' else 'album_fetch_state'
                assert preparation.original(conn, owner, row[key], row['source_digest']) == originals[row[key]]
        return {'source_tracks':len(rows),'source_states':len(states),'groups':len(groups),
            'complete_groups':sum(values[3] == 'complete' for values in groups.values()),'visible_tracks':len(visible)}
    try:
        store.init_schema(defer_metadata=True)
        other.init_schema(defer_metadata=True)
        conn.set_trace_callback(trace)
        other.conn.set_trace_callback(trace)
        prepare()
        identity = stage()
        publish('installed', identity)
        report['installed_projection'] = compare(identity)
        entries = [row[0] for row in conn.execute('SELECT e.id FROM main.catalog_entries e JOIN main.youtube_items y ON y.video_id=e.video_id ORDER BY e.id LIMIT 4096')]
        assert len(entries) == 4096
        touched = True
        with conn:
            for position, entry in enumerate(entries*2):
                added.append(conn.execute("INSERT INTO main.album_tracks(album_id,entry_id,position,is_full,source) VALUES (?,?,?,1,'controlled actual capture')",
                    (controlled_album,entry,position)).lastrowid)
            conn.execute('INSERT INTO main.album_fetch_state VALUES (?,NULL,1,8192)', (controlled_album,))
        assert publication.selected(conn) is None
        prepare()
        first = timed('copies', lambda:publication.step(conn))
        before_interruption = publication.stage(conn, first.id)
        with conn:
            conn.execute('CREATE TRIGGER controlled_actual_album_copy BEFORE INSERT ON '+publication.table('tracks')+
                " BEGIN SELECT RAISE(ABORT,'controlled copy interruption'); END")
        try:
            publication.step(conn)
        except sqlite3.IntegrityError as exc:
            assert 'controlled copy interruption' in str(exc)
        else:
            raise AssertionError('Album copy interruption was ignored')
        assert publication.stage(conn, first.id) == before_interruption
        with conn:
            conn.execute('DROP TRIGGER controlled_actual_album_copy')
        # Resume through a separately opened worker connection.
        resumed = timed('copies', lambda:publication.step(other.conn))
        assert resumed.id == first.id and resumed.copied == first.copied+32
        identity = stage()
        with conn:
            conn.execute('CREATE TRIGGER controlled_actual_album_cutover BEFORE INSERT ON '+publication.table('head')+
                " BEGIN SELECT RAISE(ABORT,'controlled cutover interruption'); END")
        try:
            publication.publish(conn, identity=identity)
        except sqlite3.IntegrityError as exc:
            assert 'controlled cutover interruption' in str(exc)
        else:
            raise AssertionError('Album cutover interruption was ignored')
        assert not conn.execute('SELECT 1 FROM '+publication.table('generations')+' WHERE revision=?', (identity,)).fetchone()
        with conn:
            conn.execute('DROP TRIGGER controlled_actual_album_cutover')
        publish('controlled', identity)
        report['controlled_projection'] = compare(identity)
        started = time.monotonic()
        assert len(other.charts.album_tracks_detail(source)) == 8192
        report['controlled_album_detail_seconds'] = time.monotonic()-started
        raced, verify = False, publication.verify
        def after_verify(connection, revision):
            nonlocal raced
            result = verify(connection, revision)
            if not raced:
                raced = True
                with other.conn:
                    other.conn.execute('UPDATE main.album_fetch_state SET fetched_at=fetched_at WHERE album_id=?', (controlled_album,))
            return result
        publication.verify = after_verify
        try:
            try:
                publication.publish(conn, identity=identity)
            except MigrationError:
                pass
            else:
                raise AssertionError('Stale album cutover was accepted')
        finally:
            publication.verify = verify
        assert publication.selected(conn) is None
        prepare()
        publish('reconciled-race', stage())
        started = time.monotonic()
        with conn:
            conn.execute("UPDATE main.album_tracks SET source='continuing exact capture' WHERE id=?", (added[0],))
        report['compatible_source_writer_seconds'] = time.monotonic()-started
        def fail(*args):
            preparation.copy(*args)
            raise OSError('controlled actual occurrence failure')
        try:
            replace(preparation.JOB, copy=fail).step(conn, batch_size=32)
        except OSError as exc:
            assert 'controlled actual occurrence failure' in str(exc)
        else:
            raise AssertionError('Injected album preparation failure did not run')
        failed = preparation.JOB.progress(conn)
        started = time.monotonic()
        store.init_schema(defer_metadata=True)
        regular.migrate(conn)
        report['installed_ddl_while_failed_seconds'] = time.monotonic()-started
        assert preparation.JOB.progress(conn) == failed and failed.status == 'failed'
        preparation.JOB.step(conn, retry=True)
        prepare()
        identity = stage()
        publish('continuing', identity)
        compare(identity)
        report.update(status='passed', interrupted_copy_resumed=True, interrupted_cutover_rolled_back=True,
            stale_publication_rejected=True, exact_originals_and_group_projection_verified=True,
            failed_job_retried_explicitly=True, preopened_readers_verified=True, continuing_writes_reconciled=True)
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        conn.set_trace_callback(None)
        other.conn.set_trace_callback(None)
        conn.rollback()
        try:
            if touched:
                with conn:
                    conn.executemany('DELETE FROM main.album_tracks WHERE id=?', [(identity,) for identity in added])
                    conn.execute('DELETE FROM main.album_fetch_state WHERE album_id=?', (controlled_album,))
            assert before == {table:fingerprint(conn, table, order) for table, order in protected.items()}
            assert history == list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
            report['original_sources_references_history_preserved_after_restore'] = True
        finally:
            report_path.write_text(json.dumps(report, indent=2))
            other.conn.close()
            conn.close()
    print(json.dumps({key:value for key,value in report.items() if key not in ('batches','copies')}, indent=2))


if __name__ == '__main__':
    check(Path(sys.argv[1]), Path(sys.argv[2]))
