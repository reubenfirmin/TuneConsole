"""Measure a controlled transfer receipt on a disposable actual library only."""
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from yt_playlist.core import collection_recovery, workflow_settings
from yt_playlist.core.migrations import regular
from yt_playlist.core.preparation import workflow_receipts as preparation


def fingerprint(conn):
    digest = hashlib.sha256()
    count = 0
    for row in conn.execute('SELECT key,value FROM settings ORDER BY key'):
        digest.update(json.dumps(tuple(row), ensure_ascii=False).encode())
        count += 1
    return count, digest.hexdigest()


def check(path, report_path):
    assert path.parent == Path('/tmp') and path.name.startswith('tuneconsole-')
    source = workflow_settings.PLAYLIST + ':9223372036854775807'
    report = {'snapshot': str(path), 'status': 'failed', 'python': sys.version, 'sqlite': sqlite3.sqlite_version,
              'dataset': 'Controlled transfer receipt containing video IDs from the actual-library copy, repeated IDs and null slots.',
              'controls': 'No HTTP/provider/runtime workers and no remote intent or identity admission.', 'batches': []}
    conn = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=.1)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    settings = fingerprint(conn)
    assert not conn.execute('SELECT 1 FROM settings WHERE key=?', (source,)).fetchone()
    job, seeded = preparation.JOBS['playlists'], False
    try:
        videos = [row[0] for row in conn.execute('SELECT video_id FROM youtube_items ORDER BY video_id LIMIT 4096')]
        assert len(videos) == 4096
        slots = [None if index % 7 == 0 else video for index, video in enumerate(videos * 2)]
        receipt = {'creation_phase': 'creating', 'marker': 'controlled-unadmitted-marker', 'complete': False,
                   'order_verified': None, 'updated_at': None, 'last_run_added': 0, 'source_video_ids': slots}
        raw = json.dumps({'controlled-transfer': receipt})
        with conn:
            conn.execute('INSERT INTO settings(key,value) VALUES (?,?)', (source, raw))
        seeded = True
        job.install(conn)
        reserved = []
        def trace(sql):
            if sql == 'BEGIN IMMEDIATE':
                reserved.append(time.monotonic())
        conn.set_trace_callback(trace)
        for index in range(150):
            reserved.clear()
            started = time.monotonic()
            progress = job.step(conn, batch_size=128)
            finished = time.monotonic()
            report['batches'].append({'seconds': finished-started, 'writer_seconds': finished-reserved[-1] if reserved else 0,
                                      'progress': asdict(progress)})
            if index == 2:
                raw += '\n'
                started = time.monotonic()
                with conn:
                    conn.execute('UPDATE settings SET value=? WHERE key=?', (raw, source))
                report['continuing_source_write_seconds'] = time.monotonic()-started
            if index == 3:
                checkpoint = job.progress(conn)
                def fail(*args):
                    preparation.copy('playlists', *args)
                    raise OSError('controlled actual workflow batch failure')
                try:
                    replace(job, copy=fail).step(conn)
                except OSError:
                    pass
                else:
                    raise AssertionError('Controlled workflow failure did not run')
                failed = job.progress(conn)
                assert failed.status == 'failed' and failed.processed == checkpoint.processed
                started = time.monotonic()
                regular.migrate(conn)
                report['already_installed_ddl_admission_while_failed_seconds'] = time.monotonic()-started
                assert job.progress(conn) == failed
                job.step(conn, retry=True)
                report['controlled_failure_explicit_retry'] = 'passed'
            if progress.status == 'prepared':
                break
        assert job.progress(conn).status == 'prepared'
        head = preparation.head(conn, 'playlists', source)
        assert preparation.read_original(conn, 'playlists', head) == raw
        captured = conn.execute('SELECT * FROM prepared_workflow_playlists_receipts WHERE source=? AND preparation_revision=?',
                                (source, head['preparation_revision'])).fetchone()
        assert captured['captured_account_id'] == 9223372036854775807 and captured['account_id'] is None
        assert captured['creation_phase'] == 'creating' and captured['updated_at'] is None
        assert [row[0] for row in conn.execute('SELECT video_id_snapshot FROM prepared_workflow_playlists_items '
            'WHERE source=? AND preparation_revision=? ORDER BY position', (source, head['preparation_revision']))] == slots
        owner = json.dumps([source, 'controlled-transfer'], separators=(',', ':'))
        assert collection_recovery.read(captured['recovery_filename'], captured['recovery_digest'], captured['recovery_bytes'],
            kind='spotify-transfer', owner=owner) == receipt
        report.update(status='passed', slots=len(slots), null_slots=slots.count(None), source_bytes=len(raw.encode()),
                      original_exact=True, ordered_slots_exact=True, recovery_exact=True)
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        conn.set_trace_callback(None)
        if seeded:
            with conn:
                conn.execute('DELETE FROM settings WHERE key=?', (source,))
            if job.progress(conn).status != 'failed':
                for _ in range(5):
                    if job.step(conn).status == 'prepared':
                        break
        report['history_exact'] = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history
        report['settings_restored_exact'] = fingerprint(conn) == settings
        report_path.write_text(json.dumps(report, indent=2))
        conn.close()
    assert report['history_exact'] and report['settings_restored_exact']


if __name__ == '__main__':
    check(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
