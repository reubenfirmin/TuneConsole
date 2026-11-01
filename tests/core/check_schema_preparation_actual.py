"""Explicit acceptance harness for an isolated populated SQLite backup.

Never collected by pytest. Supply an existing disposable snapshot; this harness
changes that copy, keeps network workers disabled and writes a JSON report.
"""
import asyncio
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time
from unittest.mock import patch

import httpx

from yt_playlist.core.migrations import regular
from yt_playlist.core.preparation import listening_subjects, radio_session
from yt_playlist.core.store import Store
from yt_playlist.web import app as web


def fingerprint(rows):
    digest = hashlib.sha256()
    for row in rows:
        digest.update(json.dumps(tuple(row), ensure_ascii=False).encode())
    return digest.hexdigest()


async def check(path, report_path):
    assert path.parent == Path('/tmp') and path.name.startswith('tuneconsole-622-')
    report = {'python': sys.version, 'sqlite': sqlite3.sqlite_version, 'snapshot': str(path),
              'bytes': path.stat().st_size, 'status': 'failed', 'requests': [], 'batches': [], 'locks': [],
              'controls': 'ASGI in-process; real schema worker; network/rec/enrichment/radio-controller workers disabled'}
    store = Store(path)
    original_acquire, original_release = store._lock.acquire, store._lock.release
    local = threading.local()

    def acquire(*args, **kwargs):
        before = time.monotonic()
        result = original_acquire(*args, **kwargs)
        after = time.monotonic()
        if result:
            if not getattr(local, 'depth', 0):
                local.start = after
                local.wait = after - before
            local.depth = getattr(local, 'depth', 0) + 1
        return result

    def release():
        if local.depth == 1:
            report['locks'].append({'thread': threading.current_thread().name,
                'hold': time.monotonic() - local.start, 'wait': local.wait})
        local.depth -= 1
        original_release()

    store._lock.acquire, store._lock.release = acquire, release
    stop = threading.Event()
    worker = None
    try:
        report['regular_changes_before'] = regular.validate(store.conn)
        started = time.monotonic()
        store.init_schema(defer_metadata=True)
        report['admission_seconds'] = time.monotonic() - started
        history = list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
        originals = {table: fingerprint(store.conn.execute('SELECT * FROM main.' + table + ' ORDER BY id'))
                     for table in ('play_events', 'listening_observations')}
        radio_rows = list(store.conn.execute("SELECT key,value FROM settings WHERE key LIKE '%radio%' ORDER BY key"))
        radio_fingerprint = fingerprint(radio_rows)
        raw = store.conn.execute('SELECT value FROM settings WHERE key=?', (radio_session.KEY,)).fetchone()[0]
        state = json.loads(raw)
        report['installed_version'] = history[-1][0]
        report['radio'] = {'bytes': len(raw.encode()), 'queue_size': len(state.get('tracks', [])),
                           'settings': len(radio_rows), 'sha256': hashlib.sha256(raw.encode()).hexdigest()}
        started = time.monotonic()
        listening_subjects.install(store.conn)
        radio_session.JOB.install(store.conn)
        report['installation_seconds'] = time.monotonic() - started
        # Keep normal startup's real bounded schema worker. Suppress unrelated
        # network/model jobs so no provider operations or serving GC can escape
        # the disposable snapshot's file roots.
        with patch.object(web, '_background_sync_loop', lambda *a, **k: None), \
             patch.object(web, '_background_update_loop', lambda *a, **k: None):
            app = web.create_app(store, lambda: {}, background_work=True)
            ctx = app.state.ctx
            ctx.rec_worker.enabled = False
            ctx.enrich_worker.start_ticker = lambda: None
            ctx.radio.prepare = lambda: None

            async def wake_loop():
                while not stop.is_set():
                    await asyncio.sleep(.01)

            heartbeat = asyncio.create_task(wake_loop())
            try:
                async with app.router.lifespan_context(app):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
                        async def requests(phase):
                            for method, route in (('GET', '/radio/status'), ('GET', '/'), ('GET', '/home/onboard/status'),
                                                  ('GET', '/storage-maintenance/status'),
                                                  ('POST', '/onboard/intro/dismiss')):
                                before = time.monotonic()
                                running_before = worker is not None and worker.is_alive()
                                response = await client.request(method, route)
                                report['requests'].append({'phase': phase, 'method': method, 'path': route,
                                    'status': response.status_code, 'seconds': time.monotonic() - before,
                                    'preparation_running_at_start': running_before,
                                    'preparation_running_at_end': worker is not None and worker.is_alive()})
                                assert response.status_code == 200, (phase, route, response.status_code, response.text[:200])
                                if route == '/radio/status':
                                    assert [t['video_id'] for t in response.json()['tracks']] == [t['video_id'] for t in state['tracks']]

                        await requests('suspended')
                        assert ctx.storage_maintenance.ready.wait(10)
                        assert ctx.storage_maintenance.phase == 'done'
                        assert regular.validate(store.conn) == len(regular.CHANGES)
                        report['regular_changes_after'] = regular.validate(store.conn)

                        def prepare():
                            report['preparation_started'] = time.monotonic()
                            try:
                                with sqlite3.connect(path, timeout=.1) as conn:
                                    conn.row_factory = sqlite3.Row
                                    conn.execute('PRAGMA foreign_keys=ON')
                                    for name, step in (('radio', radio_session.JOB.step),
                                                       ('listening', listening_subjects.step)):
                                        for _ in range(300):
                                            before = time.monotonic()
                                            result = step(conn, batch_size=1 if name == 'radio' else 128)
                                            report['batches'].append({'job': name, 'seconds': time.monotonic() - before})
                                            if result.status == 'prepared':
                                                break
                                            stop.wait(.02)
                                        assert result.status == 'prepared'
                            except BaseException as exc:
                                report['worker_error'] = repr(exc)
                            finally:
                                report['preparation_finished'] = time.monotonic()

                        worker = threading.Thread(target=prepare, name='explicit-preparation')
                        worker.start()
                        await requests('running')
                        await asyncio.to_thread(worker.join, 30)
                        assert not worker.is_alive() and 'worker_error' not in report
                        with store.conn:
                            store.conn.execute('UPDATE settings SET value=? WHERE key=?',
                                               (json.dumps({**state, 'identity': -999}), radio_session.KEY))
                        try:
                            radio_session.JOB.step(store.conn)
                        except Exception as exc:
                            report['controlled_failure'] = str(exc)
                        assert radio_session.JOB.progress(store.conn).status == 'failed'
                        await requests('failed')
                        with store.conn:
                            store.conn.execute('UPDATE settings SET value=? WHERE key=?', (raw, radio_session.KEY))
                        radio_session.JOB.step(store.conn, batch_size=2, retry=True)
                        for _ in range(300):
                            progress = radio_session.JOB.step(store.conn, batch_size=2)
                            if progress.status == 'prepared':
                                break
                        assert progress.status == 'prepared'
                        await requests('prepared')
            finally:
                stop.set()
                await heartbeat
        report['radio_progress'] = asdict(radio_session.JOB.progress(store.conn))
        report['listening_progress'] = asdict(listening_subjects.progress(store.conn))
        generation = report['radio_progress']['prepared_revision']
        head = store.conn.execute('SELECT * FROM prepared_radio_sessions WHERE generation=?', (generation,)).fetchone()
        assert radio_session.read(head) == raw
        report['radio_queue_prepared'] = store.conn.execute('SELECT count(*) FROM prepared_radio_queue WHERE generation=?', (generation,)).fetchone()[0]
        assert list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history
        assert fingerprint(store.conn.execute("SELECT key,value FROM settings WHERE key LIKE '%radio%' ORDER BY key")) == radio_fingerprint
        for table, expected in originals.items():
            assert fingerprint(store.conn.execute('SELECT * FROM main.' + table + ' ORDER BY id')) == expected
        for source, destination in (('play_events', 'prepared_listening_events'),
                                    ('listening_observations', 'prepared_listening_observations')):
            expected = {r['id']: dict(r) for r in store.conn.execute('SELECT * FROM main.' + source)}
            actual = {r['id']: json.loads(r['source_snapshot']) for r in store.conn.execute('SELECT * FROM ' + destination)}
            assert actual == expected
        report['max_lock_hold'] = max(r['hold'] for r in report['locks'])
        report['max_lock_wait'] = max(r['wait'] for r in report['locks'])
        assert not any(r['thread'] in ('schema-maintenance', 'explicit-preparation') for r in report['locks'])
        report['source_rows_exact'] = report['history_exact'] = report['radio_settings_exact'] = True
        report['status'] = 'passed'
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        stop.set()
        if worker is not None:
            worker.join(10)
        report_path.write_text(json.dumps(report, indent=2))
        store.conn.close()


if __name__ == '__main__':
    # Some restricted runners do not wake cross-thread selector notifications.
    # Keep a timer alive through executor shutdown as well as HTTP requests.
    async def main():
        async def tick():
            while True:
                await asyncio.sleep(.01)
        heartbeat = asyncio.create_task(tick())
        try:
            await check(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
        finally:
            await asyncio.get_running_loop().shutdown_default_executor(timeout=5)
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(main())
        loop.run_until_complete(loop.shutdown_asyncgens())
    finally:
        loop.close()
