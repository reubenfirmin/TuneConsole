"""Inspect action preparation on a supplied disposable actual library only."""
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from yt_playlist.core import action_backup_files, collection_recovery
from yt_playlist.core.migrations import regular
from yt_playlist.core.preparation import action_ledger as preparation


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
        'dataset': 'Isolated actual-library action history' + (' and a controlled large placement using actual video IDs.' if populated else '.'),
        'controls': 'No HTTP/provider/runtime workers, native changes or remote intent/identity admission.', 'batches': []}
    conn = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=.1)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    before = {owner: fingerprint(conn, owner, key) for owner, key in [('settings','key'),('actions','id'),('playlist_scopes','ref'),('identities','id')]}
    sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='actions'").fetchone()
    seeded = False
    job = preparation.JOB
    try:
        if populated:
            videos = [row[0] for row in conn.execute('SELECT video_id FROM youtube_items ORDER BY video_id LIMIT 4096')]
            assert len(videos) == 4096
            membership = conn.execute('SELECT id FROM playlist_membership_refs WHERE scope_ref IS NOT NULL ORDER BY id LIMIT 1').fetchone()[0]
            identity = conn.execute('SELECT COALESCE(MAX(id),0)+1 FROM actions').fetchone()[0]
            ordered = videos * 2
            plan = {'playlist_id': membership, 'video_ids': ordered, 'after_video_id': None, 'set_video_ids': {}}
            with conn:
                conn.execute('INSERT INTO actions(id,kind,status,params_json,plan_json,undo_json,created_at) VALUES (?,?,?,?,?,?,NULL)',
                    (identity, 'add_tracks', 'pending', '{}', json.dumps(plan), '{}'))
            seeded = True
        started = time.monotonic()
        job.install(conn)
        report['installation_seconds'] = time.monotonic()-started
        reservations = []
        def trace(sql):
            if sql == 'BEGIN IMMEDIATE':
                reservations.append(time.monotonic())
        conn.set_trace_callback(trace)
        changed = failed = False
        for _ in range(2000):
            reservations.clear()
            started = time.monotonic()
            progress = job.step(conn, batch_size=128)
            finished = time.monotonic()
            report['batches'].append({'seconds': finished-started, 'writer_seconds': finished-reservations[-1] if reservations else 0})
            cursor = json.loads(progress.cursor) if progress.cursor else None
            if populated and isinstance(cursor, dict) and json.loads(cursor['key']) == [identity]:
                if not changed:
                    started = time.monotonic()
                    with conn:
                        conn.execute("UPDATE actions SET plan_json=plan_json||char(10) WHERE id=?", (identity,))
                    report['continuing_write_seconds'] = time.monotonic()-started
                    changed = True
                elif not failed:
                    checkpoint = job.progress(conn)
                    def fail(*args):
                        preparation.copy(*args)
                        raise OSError('controlled actual action failure')
                    try:
                        replace(job, copy=fail).step(conn, batch_size=128)
                    except OSError:
                        pass
                    else:
                        raise AssertionError('Controlled failure did not execute')
                    failure = job.progress(conn)
                    assert failure.status == 'failed' and failure.processed == checkpoint.processed
                    started = time.monotonic()
                    regular.migrate(conn)
                    report['already_installed_ddl_admission_while_failed_seconds'] = time.monotonic()-started
                    assert job.progress(conn) == failure
                    job.step(conn, retry=True)
                    failed = True
            if progress.status == 'prepared':
                break
        conn.set_trace_callback(None)
        assert job.progress(conn).status == 'prepared'
        if populated:
            assert changed and failed
        expected = {str(row['id']): dict(row) for row in conn.execute('SELECT * FROM main.actions')}
        assert {row[0] for row in conn.execute('SELECT source FROM prepared_action_heads')} == set(expected)
        backups = verified = missing = uncertain = members = 0
        for source, value in expected.items():
            head = preparation.head(conn, source)
            assert preparation.read_original(conn, head) == value
            assert collection_recovery.read(head['recovery_filename'], head['recovery_digest'], head['recovery_bytes'], kind='action', owner=source) == {
                'params': value['params_json'], 'plan': value['plan_json'], 'undo': value['undo_json']}
            for row in conn.execute('SELECT * FROM prepared_action_backups WHERE source=? AND preparation_revision=? ORDER BY position', (source, head['preparation_revision'])):
                backups += 1
                if row['backup_status'] == 'verified':
                    action_backup_files.verified(row['retained_filename'], row['backup_digest'], row['backup_bytes'])
                    verified += 1
                else:
                    assert row['backup_status'] == 'missing_historical'
                    missing += 1
            undo = json.loads(value['undo_json']) if value['undo_json'] and value['kind'] != 'merge_draft' else None
            for position, step in enumerate(undo.get('deletions', []) if isinstance(undo, dict) else []):
                staged = conn.execute('SELECT phase,outcome FROM prepared_action_steps WHERE source=? AND preparation_revision=? AND position=?',
                                      (source, head['preparation_revision'], position)).fetchone()
                assert staged is not None
                if step.get('phase', 'pending') == 'pending':
                    assert tuple(staged) == ('uncertain', 'unknown')
                    uncertain += 1
            plan = json.loads(value['plan_json']) if value['plan_json'] and value['kind'] != 'merge_draft' else None
            if isinstance(plan, dict) and type(plan.get('playlist_id')) is int:
                captured = conn.execute('SELECT video_id_snapshot FROM prepared_action_placement_items WHERE source=? AND preparation_revision=? ORDER BY position',
                    (source, head['preparation_revision'])).fetchall()
                assert [row[0] for row in captured] == plan.get('video_ids', [])
                members += len(captured)
        report.update(status='passed', source_count=len(expected), originals_exact=True, recovery_exact=True,
            backups=backups, verified_backups=verified, missing_historical_backups=missing,
            pending_deletions_preserved_as_uncertain=uncertain, placement_members=members, progress=asdict(job.progress(conn)))
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        conn.set_trace_callback(None)
        if seeded:
            with conn:
                conn.execute('DELETE FROM actions WHERE id=?', (identity,))
                if sequence is not None:
                    conn.execute("UPDATE sqlite_sequence SET seq=? WHERE name='actions'", (sequence[0],))
                else:
                    conn.execute("DELETE FROM sqlite_sequence WHERE name='actions'")
            if job.progress(conn).status != 'failed':
                for _ in range(10):
                    if job.step(conn).status == 'prepared':
                        break
        report['history_exact'] = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history
        report['source_owners_restored_exact'] = before == {owner: fingerprint(conn, owner, key) for owner, key in [('settings','key'),('actions','id'),('playlist_scopes','ref'),('identities','id')]}
        report['sequence_restored_exact'] = tuple(conn.execute("SELECT seq FROM sqlite_sequence WHERE name='actions'").fetchone() or ()) == tuple(sequence or ())
        report_path.write_text(json.dumps(report, indent=2))
        conn.close()
    assert report['history_exact'] and report['source_owners_restored_exact'] and report['sequence_restored_exact']


if __name__ == '__main__':
    check(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), populated='--populated' in sys.argv[3:])
