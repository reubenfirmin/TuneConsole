"""Run the real trend pipeline on an explicitly supplied disposable library.

Never collected by pytest. No provider or application background workers run.
The disposable database's trend cache changes; source evidence and numbered
migration history must stay exact. The native database is never opened here.
"""
import faulthandler
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from yt_playlist.core.activity import work_budget
from yt_playlist.core.store import Store
from yt_playlist.rec import trend_rollups


def fingerprint(conn, table):
    digest = hashlib.sha256()
    count = 0
    for row in conn.execute('SELECT * FROM main.' + table + ' ORDER BY id'):
        digest.update(json.dumps(tuple(row), ensure_ascii=False).encode())
        count += 1
    return {'count': count, 'sha256': digest.hexdigest()}


def check(path, report_path):
    assert path.parent == Path('/tmp') and path.name.startswith('tuneconsole-')
    report = {'snapshot': str(path), 'bytes': path.stat().st_size, 'status': 'failed',
              'python': sys.version, 'sqlite': sqlite3.sqlite_version,
              'controls': 'Real trend snapshot/calculation/publication; no network or background workers.'}
    store = Store(path)
    try:
        store.init_schema(defer_metadata=True)
        history = list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
        sources = {table: fingerprint(store.conn, table)
                   for table in ('play_events', 'listening_observations')}
        before = time.monotonic()
        with work_budget(60):
            payload = trend_rollups.build(store, time.time())
        report['build_seconds'] = time.monotonic() - before
        assert payload is not None
        assert store.rec_proposals.get_proposals('trend_rollups') == payload
        report['first_play_tracks'] = len(store.trends.first_play_map('track'))
        report['first_play_artists'] = len(store.trends.first_play_map('artist'))
        assert report['first_play_tracks'] and report['first_play_artists']
        assert list(map(tuple, store.conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history
        assert {table: fingerprint(store.conn, table) for table in sources} == sources
        report.update(status='passed', historical_version=history[-1][0], history_exact=True,
                      evidence_exact=True, sources=sources)
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        report_path.write_text(json.dumps(report, indent=2))
        store.conn.close()


if __name__ == '__main__':
    faulthandler.dump_traceback_later(90, exit=True)
    try:
        check(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
    finally:
        faulthandler.cancel_dump_traceback_later()
