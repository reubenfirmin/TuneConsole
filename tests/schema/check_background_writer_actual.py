"""Opt-in writer-admission overlap on a disposable actual-library cohort."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time

import pytest

from tests.schema.actual_library import copy_readonly_library
from tests.library_image_fixtures import report_file
from yt_playlist.core import radio_storage
from yt_playlist.core.activity import check_cancellable_work, work_budget
from yt_playlist.core.store import Store
from yt_playlist.rec import choice_evidence


def fingerprint(conn, table):
    digest, count = hashlib.sha256(), 0
    for row in conn.execute('SELECT * FROM ' + table + ' ORDER BY id'):
        digest.update(repr(tuple(row)).encode())
        count += 1
    return count, digest.hexdigest()


def test_actual_library_background_writers_remain_useful_and_serve_reads(request, tmp_path, monkeypatch):
    option = request.config.getoption('--schema-source-db')
    if not option:
        pytest.skip('Supply a read-only source for an isolated SQL and file copy')
    path = Path(option).resolve(strict=True)
    destination = tmp_path / 'isolated.db'
    report_path = report_file('/tmp/tuneconsole-677-actual.json')
    report = {'source': str(path), 'source_access': 'SQLite mode=ro; files read only',
              'python': sys.version, 'sqlite': sqlite3.sqlite_version, 'status': 'copying',
              'latencies_seconds': {}, 'workers': {}, 'source_hashes': {}}
    for name in ('core/db_writes.py', 'core/db_snapshot.py', 'rec/group_roots.py',
                 'repos/enrichment_state.py', 'core/store.py', 'rec/choice_evidence.py'):
        source = Path(__file__).resolve().parents[2] / 'src/yt_playlist' / name
        report['source_hashes'][name] = hashlib.sha256(source.read_bytes()).hexdigest()
    store = None
    try:
        destination = copy_readonly_library(path, destination, monkeypatch, report)
        store = Store(destination)
        store.init_schema(defer_metadata=True)
        before = {table: fingerprint(store.conn, table) for table in ('recordings', 'play_events')}
        history = list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
        radio = radio_storage.load(store.conn)
        report['recordings'] = before['recordings'][0]
        report['radio_tracks'] = len((radio[0] or {}).get('tracks', []))
        configuration = {row['provider']: {'fingerprint': row['fingerprint'], 'status': row['status']}
                         for row in store.conn.execute('SELECT * FROM enrichment_provider_config')}
        provider = 'deezer'
        configuration[provider] = {'fingerprint': 'writer-admission-actual-revision', 'status': 'available'}
        attempted = threading.Event()
        store.conn.set_trace_callback(lambda sql: attempted.set() if sql == 'BEGIN IMMEDIATE' else None)
        report['status'] = 'running'
        def run(name, callback):
            started = time.monotonic()
            with store.priority.background(check_cancellable_work), work_budget(120):
                result = callback()
            report['workers'][name] = {'seconds': time.monotonic() - started, 'result': result}
            return result
        def serve(phase):
            started = time.monotonic()
            with store._lock:
                assert store.conn.execute('SELECT count(*) FROM enrichment_provider_config').fetchone()[0]
                assert store.conn.execute('SELECT count(*) FROM schema_migrations').fetchone()[0] == len(history)
            report['latencies_seconds'].setdefault(phase, []).append(time.monotonic() - started)
        with closing(sqlite3.connect(destination)) as blocker, ThreadPoolExecutor(2) as workers:
            blocker.execute('BEGIN IMMEDIATE')
            enrichment = workers.submit(run, 'reconciliation',
                lambda: store.enrichment_state.reconcile_provider_configuration(configuration))
            maintenance = workers.submit(run, 'evidence-maintenance',
                lambda: choice_evidence.maintain_storage(store, time.time()))
            try:
                assert attempted.wait(3)
                for _ in range(8):
                    serve('competing-writer')
            finally:
                blocker.rollback()
            while not enrichment.done() or not maintenance.done():
                serve('useful-overlap')
                time.sleep(.01)
            enrichment.result()
            maintenance.result()
        # A budget-limited inventory keeps its durable prefix and gets another
        # useful turn after admission, rather than leaving the connection stuck.
        run('evidence-retry', lambda: choice_evidence.maintain_storage(store, time.time()))
        assert store.conn.execute('SELECT fingerprint FROM enrichment_provider_config WHERE provider=?',
                                  (provider,)).fetchone()[0] == configuration[provider]['fingerprint']
        report['provider_jobs'] = store.conn.execute(
            'SELECT count(*) FROM enrichment_provider_jobs WHERE provider=?', (provider,)).fetchone()[0]
        assert before == {table: fingerprint(store.conn, table) for table in before}
        assert history == list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
        assert radio_storage.load(store.conn) == radio
        assert not store.conn.in_transaction
        assert store.conn.execute('PRAGMA busy_timeout').fetchone()[0] == 5000
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
        for samples in report['latencies_seconds'].values():
            assert samples and max(samples) < 1
        report.update(status='passed', history_exact=True, radio_exact=True, source_records_exact=True)
    except BaseException as error:
        report.update(status='failed', error=repr(error))
        raise
    finally:
        if store is not None:
            store.conn.set_trace_callback(None)
            store.conn.close()
        report_path.write_text(json.dumps(report, indent=2, default=str) + '\n')
