"""Measure controlled taste comparison preparation on a disposable real catalog.

Never collected by pytest. The controlled setting is removed, installed settings
and numbered history are checked exactly, and no native DB or provider is used.
"""
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from yt_playlist.core import collection_recovery
from yt_playlist.core.migrations import regular
from yt_playlist.core.preparation import taste_controls as preparation


def fingerprint(conn):
    digest = hashlib.sha256()
    count = 0
    for row in conn.execute('SELECT key,value FROM settings ORDER BY key'):
        digest.update(json.dumps(tuple(row), ensure_ascii=False).encode())
        count += 1
    return count, digest.hexdigest()


def check(path, report_path):
    assert path.parent == Path('/tmp') and path.name.startswith('tuneconsole-')
    report = {'snapshot': str(path), 'status': 'failed', 'python': sys.version, 'sqlite': sqlite3.sqlite_version,
              'dataset': 'Controlled ordered comparison using 8192 exact identities from the actual-library copy.',
              'controls': 'No HTTP, provider clients or runtime background workers.', 'batches': []}
    conn = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=.1)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    settings = fingerprint(conn)
    assert not conn.execute('SELECT 1 FROM settings WHERE key=?', (preparation.BASELINE,)).fetchone()
    job, seeded = preparation.JOBS['baseline'], False
    try:
        keys = [row[0] for row in conn.execute('SELECT identity_key FROM recordings ORDER BY id LIMIT 8192')]
        assert len(keys) == 8192
        value = [{'key': key, 'rank': index + 1, 'score': 1 / (index + 1), 'lane': '', 'lane_weight': None,
                  'trace': {'controlled_rank': index, 'nullable': None}} for index, key in enumerate(keys)]
        raw = json.dumps(value, ensure_ascii=False)
        with conn:
            conn.execute('INSERT INTO settings(key,value) VALUES (?,?)', (preparation.BASELINE, raw))
        seeded = True
        job.install(conn)
        reserved = []
        def trace(sql):
            if sql == 'BEGIN IMMEDIATE':
                reserved.append(time.monotonic())
        conn.set_trace_callback(trace)
        for index in range(200):
            reserved.clear()
            started = time.monotonic()
            progress = job.step(conn, batch_size=128)
            finished = time.monotonic()
            report['batches'].append({'seconds': finished - started,
                'writer_seconds': finished - reserved[-1] if reserved else 0, 'progress': asdict(progress)})
            if index == 2:
                # Preserve parsed meaning but change exact source bytes while
                # its older generation is only partially copied.
                raw += '\n'
                started = time.monotonic()
                with conn:
                    conn.execute('UPDATE settings SET value=? WHERE key=?', (raw, preparation.BASELINE))
                report['continuing_source_write_seconds'] = time.monotonic() - started
            if index == 3:
                checkpoint = job.progress(conn)
                def fail(*args):
                    preparation.copy(*args)
                    raise OSError('controlled actual taste batch failure')
                try:
                    replace(job, copy=fail).step(conn)
                except OSError:
                    pass
                else:
                    raise AssertionError('Controlled failure did not run')
                failed = job.progress(conn)
                assert failed.status == 'failed' and failed.processed == checkpoint.processed
                started = time.monotonic()
                regular.migrate(conn)
                report['already_installed_ddl_admission_while_failed_seconds'] = time.monotonic() - started
                assert job.progress(conn) == failed
                job.step(conn, retry=True)
                report['controlled_failure_explicit_retry'] = 'passed'
            if progress.status == 'prepared':
                break
        assert job.progress(conn).status == 'prepared'
        head = preparation.head(conn, preparation.BASELINE)
        assert preparation.read_original(conn, head) == raw
        entries = conn.execute('SELECT captured_key,rank,score,lane,lane_weight FROM prepared_taste_comparison_entries '
            'WHERE source=? AND preparation_revision=? ORDER BY position', (preparation.BASELINE, head['preparation_revision'])).fetchall()
        assert list(map(tuple, entries)) == [(item['key'], item['rank'], item['score'], item['lane'], item['lane_weight']) for item in value]
        assert collection_recovery.read(head['recovery_filename'], head['recovery_digest'], head['recovery_bytes'],
            kind='taste-comparison', owner=1) == [item['trace'] for item in value]
        report.update(status='passed', members=len(entries), source_bytes=len(raw.encode()), original_exact=True,
                      ordered_entries_exact=True, traces_exact=True)
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        conn.set_trace_callback(None)
        if seeded:
            with conn:
                conn.execute('DELETE FROM settings WHERE key=?', (preparation.BASELINE,))
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
