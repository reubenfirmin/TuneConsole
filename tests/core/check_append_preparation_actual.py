"""Measure append custody on a supplied disposable actual library only."""
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from yt_playlist.core import collection_recovery
from yt_playlist.core.migrations import regular
from yt_playlist.core.preparation import append_operations as preparation


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
        'dataset': 'Isolated actual library' + (' with controlled populated operations using actual video IDs.' if populated else '.'),
        'controls': 'No HTTP/provider/runtime workers, native changes or remote intent admission.', 'jobs': {}}
    conn = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=.1)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    version = history[-1][0]
    assert version == 93, 'This actual-library check requires its genuine v93 snapshot'
    original_settings = fingerprint(conn, 'settings', 'key')
    original_recipes = fingerprint(conn, 'rec_recipes', 'playlist_ytm')
    available_scope = conn.execute('SELECT ref FROM main.playlist_scopes s WHERE NOT EXISTS '
        '(SELECT 1 FROM rec_recipes r WHERE r.playlist_ytm=s.ref) ORDER BY ref DESC LIMIT 1').fetchone()
    assert available_scope is not None
    source = available_scope[0]
    original_radio = conn.execute("SELECT value FROM settings WHERE key='radio_current_session'").fetchone()
    assert not conn.execute('SELECT 1 FROM rec_recipes WHERE playlist_ytm=?', (source,)).fetchone()
    seeded = False
    try:
        if populated:
            videos = [row[0] for row in conn.execute('SELECT video_id FROM youtube_items ORDER BY video_id LIMIT 4096')]
            assert len(videos) == 4096
            ordered = videos * 2
            recipe = {'tv_stream': 'controlled-unadmitted-stream', 'tv_refill': {'request_id': 'controlled-request', 'state': 'pending',
                'before': ordered, 'additions': [{'video_id': video} for video in ordered], 'items': [], 'at': None}}
            radio = {'version': 2, 'episode_id': 'controlled-unadmitted-episode', 'identity': None, 'phase': 'replacing',
                'pending': None, 'replacement': {'allowed': [[], ordered, []], 'confirmed': ordered[:3], 'waiting': [],
                'waiting_at': None, 'receipt': {'status': 'UNKNOWN', 'opaque': {'paid': 'captured'}}, 'adding': None,
                'proposed': [{'video_id': video} for video in ordered], 'play_prefix': None, 'slot': 0, 'choice_id': None}}
            with conn:
                conn.execute('INSERT INTO rec_recipes(playlist_ytm,recipe,created_at) VALUES (?,?,NULL)', (source, json.dumps(recipe)))
                conn.execute("INSERT INTO settings(key,value) VALUES ('radio_current_session',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (json.dumps(radio),))
            seeded = True
        for name in ('tv_legacy', 'radio_legacy'):
            job = preparation.JOBS[name]
            started = time.monotonic()
            job.install(conn)
            detail = report['jobs'][name] = {'installation_seconds': time.monotonic()-started, 'batches': []}
            reservations = []
            def trace(sql):
                if sql == 'BEGIN IMMEDIATE':
                    reservations.append(time.monotonic())
            conn.set_trace_callback(trace)
            changed = failed = False
            for _ in range(1000):
                reservations.clear()
                started = time.monotonic()
                progress = job.step(conn, batch_size=128)
                finished = time.monotonic()
                detail['batches'].append({'seconds': finished-started, 'writer_seconds': finished-reservations[-1] if reservations else 0})
                cursor = json.loads(progress.cursor) if progress.cursor else None
                target = source if name == 'tv_legacy' else 'radio_current_session'
                if populated and isinstance(cursor, dict) and target in cursor['key']:
                    if not changed:
                        owner, key, field = ('rec_recipes', 'playlist_ytm', 'recipe') if name == 'tv_legacy' else ('settings', 'key', 'value')
                        started = time.monotonic()
                        with conn:
                            conn.execute(f'UPDATE {owner} SET {field}={field}||char(10) WHERE {key}=?', (target,))
                        detail['continuing_write_seconds'] = time.monotonic()-started
                        changed = True
                    elif not failed:
                        checkpoint = job.progress(conn)
                        def fail(*args):
                            preparation.copy(name, *args)
                            raise OSError('controlled actual append failure')
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
                        detail['already_installed_ddl_admission_while_failed_seconds'] = time.monotonic()-started
                        assert job.progress(conn) == failure
                        job.step(conn, batch_size=128, retry=True)
                        failed = True
                if progress.status == 'prepared':
                    break
            conn.set_trace_callback(None)
            assert job.progress(conn).status == 'prepared'
            if populated:
                assert changed and failed
            owner, key, _, _ = preparation.SOURCES[name]
            raw_field = 'recipe' if name == 'tv_legacy' else 'value'
            expected = dict(conn.execute(f'SELECT {key},{raw_field} FROM main.{owner} WHERE ' + (job.definition._filter(owner) or '1')))
            assert {row[0] for row in conn.execute(f'SELECT source FROM {preparation.table(name,"heads")}')} == set(expected)
            operations = members = 0
            for captured_source, raw in expected.items():
                head = preparation.head(conn, name, captured_source)
                assert preparation.read_original(conn, name, head) == raw
                value = json.loads(raw) if raw else None
                active = bool(value and (value.get('tv_refill') is not None if name == 'tv_legacy' else value.get('pending') or value.get('replacement')))
                assert head['operation_present'] == int(active)
                operations += int(active)
                if not active:
                    continue
                checkpoint = value['tv_refill'] if name == 'tv_legacy' else value
                assert collection_recovery.read(head['recovery_filename'], head['recovery_digest'], head['recovery_bytes'],
                    kind='tv-append' if name == 'tv_legacy' else 'radio-session', owner=captured_source) == checkpoint
                stored = list(map(tuple, conn.execute(f'SELECT role,list_position,position,video_id_snapshot FROM {preparation.table(name,"items")} '
                    'WHERE source=? AND preparation_revision=? ORDER BY role,list_position,position', (captured_source, head['preparation_revision']))))
                ordered_members = []
                if name == 'tv_legacy':
                    for role in ('before', 'additions', 'items'):
                        ordered_members.extend((role, 0, index, item if role == 'before' else item['video_id']) for index, item in enumerate(checkpoint.get(role, [])))
                    assert head['state'] == checkpoint['state'] and head['started_at'] == checkpoint.get('at')
                elif checkpoint.get('replacement'):
                    operation = checkpoint['replacement']
                    for index, sequence in enumerate(operation.get('allowed', [])):
                        ordered_members.extend(('allowed', index, position, video) for position, video in enumerate(sequence))
                    for role in ('confirmed', 'waiting', 'play_prefix', 'excluded', 'proposed'):
                        ordered_members.extend((role, 0, index, item['video_id'] if role == 'proposed' else item)
                            for index, item in enumerate(operation.get(role) or []))
                    assert head['allowed_count'] == len(operation['allowed']) and head['sent'] == int(operation.get('waiting') is not None)
                assert stored == sorted(ordered_members)
                members += len(stored)
            detail.update(source_count=len(expected), operations=operations, members=members, originals_exact=True, recovery_exact=True,
                          ordered_members_exact=True, progress=asdict(job.progress(conn)))
        report['status'] = 'passed'
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        conn.set_trace_callback(None)
        if seeded:
            with conn:
                conn.execute('DELETE FROM rec_recipes WHERE playlist_ytm=?', (source,))
                if original_radio is None:
                    conn.execute("DELETE FROM settings WHERE key='radio_current_session'")
                else:
                    conn.execute("UPDATE settings SET value=? WHERE key='radio_current_session'", (original_radio[0],))
            for name in ('tv_legacy', 'radio_legacy'):
                job = preparation.JOBS[name]
                if conn.execute('SELECT 1 FROM sqlite_schema WHERE name=?', (job.state,)).fetchone() and job.progress(conn).status != 'failed':
                    for _ in range(10):
                        if job.step(conn).status == 'prepared':
                            break
        report['history_exact'] = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history
        report['settings_restored_exact'] = fingerprint(conn, 'settings', 'key') == original_settings
        report['recipes_restored_exact'] = fingerprint(conn, 'rec_recipes', 'playlist_ytm') == original_recipes
        report_path.write_text(json.dumps(report, indent=2))
        conn.close()
    assert report['history_exact'] and report['settings_restored_exact'] and report['recipes_restored_exact']


if __name__ == '__main__':
    check(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), populated='--populated' in sys.argv[3:])
