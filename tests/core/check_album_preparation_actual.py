"""Measure retained album rows/shares on a disposable actual-library copy."""
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from yt_playlist.core.migrations import regular
from yt_playlist.core.preparation import album_occurrences as rows, album_shares as shares


def fingerprint(conn, owner, key):
    digest = hashlib.sha256()
    count = 0
    for row in conn.execute(f'SELECT * FROM main.{owner} ORDER BY {key}'):
        digest.update(json.dumps(tuple(row), ensure_ascii=False).encode())
        count += 1
    return count, digest.hexdigest()


def check(path, report_path, *, populated=False):
    assert path.parent == Path('/tmp') and path.name.startswith('tuneconsole-')
    report = {'snapshot': str(path), 'status': 'failed', 'python': sys.version, 'sqlite': sqlite3.sqlite_version,
        'dataset': 'Isolated actual-library album rows and shares' + (' with a controlled populated share using actual video IDs.' if populated else '.'),
        'controls': 'No HTTP/provider/runtime workers, native changes or identity admission.', 'jobs': {}}
    conn = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=.1)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    owners = [('album_tracks','id'), ('album_fetch_state','album_id'), ('album_share_snapshots','browse_id'),
              ('albums','id'), ('album_provider_ids','provider,provider_id'), ('settings','key')]
    before = {owner: fingerprint(conn, owner, key) for owner, key in owners}
    history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    catalog_count = conn.execute('SELECT count(*) FROM catalog_entries').fetchone()[0]
    seeded = False
    try:
        if populated:
            source = conn.execute("SELECT provider_id FROM album_provider_ids p WHERE provider='youtube' AND NOT EXISTS "
                '(SELECT 1 FROM album_share_snapshots s WHERE s.browse_id=p.provider_id) ORDER BY provider_id DESC LIMIT 1').fetchone()[0]
            videos = [row[0] for row in conn.execute('SELECT video_id FROM youtube_items ORDER BY video_id LIMIT 4096')]
            assert len(videos) == 4096
            ordered = [video if index % 7 else '@controlled-unresolved-album-source' for index, video in enumerate(videos * 2)]
            with conn:
                conn.execute('INSERT INTO album_share_snapshots VALUES (?,?,?)', (source, 'Controlled frozen share', json.dumps(ordered)))
            seeded = True
        for name, job in [('rows', rows.JOB), ('shares', shares.JOB)]:
            started = time.monotonic()
            job.install(conn)
            detail = report['jobs'][name] = {'installation_seconds': time.monotonic()-started, 'batches': []}
            reservations = []
            def trace(sql):
                if sql == 'BEGIN IMMEDIATE':
                    reservations.append(time.monotonic())
            conn.set_trace_callback(trace)
            changed = failed = False
            for index in range(1500):
                reservations.clear()
                started = time.monotonic()
                progress = job.step(conn, batch_size=128)
                finished = time.monotonic()
                detail['batches'].append({'seconds': finished-started, 'writer_seconds': finished-reservations[-1] if reservations else 0})
                cursor = json.loads(progress.cursor) if progress.cursor else None
                if populated and name == 'shares' and isinstance(cursor, dict) and json.loads(cursor['key']) == [source]:
                    if not changed:
                        started = time.monotonic()
                        with conn:
                            conn.execute('UPDATE album_share_snapshots SET video_ids_json=video_ids_json||char(10) WHERE browse_id=?', (source,))
                        detail['continuing_write_seconds'] = time.monotonic()-started
                        changed = True
                    elif not failed:
                        checkpoint = job.progress(conn)
                        def fail(*args):
                            shares.copy(*args)
                            raise OSError('controlled actual album share failure')
                        try:
                            replace(job, copy=fail).step(conn)
                        except OSError:
                            pass
                        else:
                            raise AssertionError('Controlled failure did not run')
                        failure = job.progress(conn)
                        assert failure.status == 'failed' and failure.processed == checkpoint.processed
                        started = time.monotonic()
                        regular.migrate(conn)
                        detail['already_installed_ddl_admission_while_failed_seconds'] = time.monotonic()-started
                        assert job.progress(conn) == failure
                        job.step(conn, retry=True)
                        failed = True
                if progress.status == 'prepared':
                    break
            conn.set_trace_callback(None)
            assert job.progress(conn).status == 'prepared'
            if populated and name == 'shares':
                assert changed and failed
            detail['progress'] = asdict(job.progress(conn))
        expected = {row['id']: dict(row) for row in conn.execute('SELECT * FROM main.album_tracks')}
        actual = list(conn.execute('SELECT * FROM prepared_album_track_rows'))
        assert len(actual) == len(expected)
        for row in actual:
            original = expected[row['row_id']]
            assert rows.original(conn, 'album_tracks', row['row_id'], row['source_digest']) == original
            assert (row['album_id'], row['is_full'], row['entry_id'], row['captured_recording_id'], row['position'], row['disc_number'], row['track_number']) == tuple(
                original[field] for field in ('album_id','is_full','entry_id','recording_id','position','disc_number','track_number'))
        states = list(conn.execute('SELECT * FROM prepared_album_fetch_rows'))
        assert len(states) == conn.execute('SELECT count(*) FROM main.album_fetch_state').fetchone()[0]
        for row in states:
            original = dict(conn.execute('SELECT * FROM main.album_fetch_state WHERE album_id=?', (row['album_id'],)).fetchone())
            assert rows.original(conn, 'album_fetch_state', row['album_id'], row['source_digest']) == original
        source_shares = {row['browse_id']: dict(row) for row in conn.execute('SELECT * FROM main.album_share_snapshots')}
        assert {row[0] for row in conn.execute('SELECT source FROM prepared_album_share_heads')} == set(source_shares)
        members = unresolved = 0
        for share_source, original in source_shares.items():
            head = shares.head(conn, share_source)
            assert shares.read_original(conn, head) == original
            captured = conn.execute('SELECT position,raw_source_ref,entry_id,recording_id FROM prepared_album_share_items '
                'WHERE source=? AND preparation_revision=? ORDER BY position', (share_source, head['preparation_revision'])).fetchall()
            assert [(row['position'], row['raw_source_ref']) for row in captured] == list(enumerate(json.loads(original['video_ids_json'])))
            for row in captured:
                entry = conn.execute('SELECT id,recording_id FROM catalog_entries WHERE video_id=?', (row['raw_source_ref'],)).fetchone()
                assert (row['entry_id'], row['recording_id']) == (tuple(entry) if entry else (None, None))
                unresolved += int(entry is None)
            members += len(captured)
        report.update(status='passed', source_rows=len(actual), full_rows=sum(row['is_full'] for row in actual),
            incidental_rows=sum(not row['is_full'] for row in actual), fetch_states=len(states), source_shares=len(source_shares),
            share_members=members, unresolved_share_members=unresolved, originals_exact=True, ordered_choices_exact=True)
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        conn.set_trace_callback(None)
        if seeded:
            with conn:
                conn.execute('DELETE FROM album_share_snapshots WHERE browse_id=?', (source,))
            if shares.JOB.progress(conn).status != 'failed':
                for _ in range(10):
                    if shares.JOB.step(conn).status == 'prepared':
                        break
        report['source_owners_restored_exact'] = before == {owner: fingerprint(conn, owner, key) for owner, key in owners}
        report['history_exact'] = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history
        report['catalog_count_exact'] = conn.execute('SELECT count(*) FROM catalog_entries').fetchone()[0] == catalog_count
        report_path.write_text(json.dumps(report, indent=2))
        conn.close()
    assert report['source_owners_restored_exact'] and report['history_exact'] and report['catalog_count_exact']


if __name__ == '__main__':
    check(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), populated='--populated' in sys.argv[3:])
