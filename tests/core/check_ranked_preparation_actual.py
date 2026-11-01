"""Measure a controlled paired projection using a disposable actual catalog.

Never collected by pytest. Retains exact installed settings/history/proposals;
the controlled source is removed afterwards. No native DB or provider is used.
"""
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from yt_playlist.core.migrations import regular
from yt_playlist.core.preparation import ranked_state


def fingerprint(conn, query):
    digest = hashlib.sha256()
    count = 0
    for row in conn.execute(query):
        digest.update(json.dumps(tuple(row), ensure_ascii=False).encode())
        count += 1
    return count, digest.hexdigest()


def check(path, report_path):
    assert path.parent == Path('/tmp') and path.name.startswith('tuneconsole-')
    surface = 'model_pair:9223372036854775807:challenger:radio'
    report = {'snapshot': str(path), 'status': 'failed', 'python': sys.version, 'sqlite': sqlite3.sqlite_version,
              'dataset': 'Controlled paired projection using exact identities and one policy from the actual-library copy.',
              'batches': [], 'controls': 'No HTTP/provider workers; request evidence is recorded separately.'}
    conn = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=.1)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    settings = fingerprint(conn, 'SELECT key,value FROM settings ORDER BY key')
    proposals = fingerprint(conn, 'SELECT surface,payload,built_at FROM rec_proposals ORDER BY surface')
    assert not conn.execute('SELECT 1 FROM rec_proposals WHERE surface=?', (surface,)).fetchone()
    seeded = False
    job = ranked_state.JOBS['pairs']
    try:
        keys = [row[0] for row in conn.execute('SELECT identity_key FROM recordings ORDER BY id LIMIT 8192')]
        assert len(keys) > 1000
        policy = conn.execute('SELECT version FROM recommendation_policies ORDER BY created_at DESC,version LIMIT 1').fetchone()[0]
        items = [{'key': key, 'score': 1 / (position + 1), 'pool_policy': policy} for position, key in enumerate(keys)]
        value = {'run_id': 9223372036854775807, 'data': {'policy_version': policy,
            'modes': [{'mode_id': 9223372036854775807, 'space': '', 'centroid': [1., 0.], 'label': 'Captured'}],
            'bundles': {'captured': {'wheelhouse': items}, '_radio': {'space': '', 'members': {'captured': keys},
                'scores': {key: .5 for key in keys}}, '_ppr': {'captured': list(reversed(keys))}}}}
        raw = json.dumps(value, ensure_ascii=False)
        with conn:
            conn.execute('INSERT INTO rec_proposals VALUES (?,?,NULL)', (surface, raw))
        seeded = True
        job.install(conn)
        reservations = []
        def trace(sql):
            if sql == 'BEGIN IMMEDIATE':
                reservations.append(time.monotonic())
        conn.set_trace_callback(trace)
        for index in range(400):
            reservations.clear()
            started = time.monotonic()
            progress = job.step(conn, batch_size=128)
            finished = time.monotonic()
            report['batches'].append({'seconds': finished - started,
                'writer_seconds': finished - reservations[-1] if reservations else 0,
                'progress': asdict(progress)})
            if index == 2:
                assert ranked_state.head(conn, surface) is None
                before = time.monotonic()
                with conn:
                    conn.execute('UPDATE rec_proposals SET built_at=22 WHERE surface=?', (surface,))
                report['continuing_source_write_seconds'] = time.monotonic() - before
            if index == 3:
                checkpoint = job.progress(conn)
                def fail(*args):
                    ranked_state.copy(*args)
                    raise OSError('controlled actual-catalog batch failure')
                try:
                    replace(job, copy=fail).step(conn, batch_size=128)
                except OSError:
                    pass
                else:
                    raise AssertionError('Controlled failure did not run')
                failed = job.progress(conn)
                assert failed.status == 'failed' and failed.processed == checkpoint.processed
                before = time.monotonic()
                regular.migrate(conn)
                report['ordinary_ddl_admission_while_failed_seconds'] = time.monotonic() - before
                assert job.progress(conn) == failed
                assert tuple(conn.execute('SELECT payload,built_at FROM rec_proposals WHERE surface=?', (surface,)).fetchone()) == (raw, 22)
                job.step(conn, batch_size=128, retry=True)
                report['controlled_failure_explicit_retry'] = 'passed'
            if progress.status == 'prepared':
                break
        assert job.progress(conn).status == 'prepared'
        head = ranked_state.head(conn, surface)
        assert head['built_at'] == 22 and head['run_resolution'] == 'missing_run'
        assert ranked_state.read_original(conn, head) == raw
        count = conn.execute('SELECT count(*) FROM prepared_model_pairs_candidates WHERE surface=? AND preparation_revision=?',
                             (surface, head['preparation_revision'])).fetchone()[0]
        assert count == len(keys) * 4
        report.update(status='passed', recordings=len(keys), candidates=count, source_bytes=len(raw.encode()),
                      original_exact=True, observation_time_exact=True)
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        conn.set_trace_callback(None)
        if seeded:
            with conn:
                conn.execute('DELETE FROM rec_proposals WHERE surface=?', (surface,))
            if job.progress(conn).status != 'failed':
                for _ in range(10):
                    if job.step(conn).status == 'prepared':
                        break
        report['history_exact'] = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history
        report['settings_exact'] = fingerprint(conn, 'SELECT key,value FROM settings ORDER BY key') == settings
        report['proposals_restored_exact'] = fingerprint(conn, 'SELECT surface,payload,built_at FROM rec_proposals ORDER BY surface') == proposals
        report_path.write_text(json.dumps(report, indent=2))
        conn.close()
    assert report['history_exact'] and report['settings_exact'] and report['proposals_restored_exact']


if __name__ == '__main__':
    check(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
