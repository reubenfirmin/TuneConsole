"""Opt-in #506 output/latency audit; source read-only, all writes on a backup.

uv run pytest --test-lock-wait=60 -s tests/bench_analytics_reads.py
TUNECONSOLE_BENCH_DB selects an existing actual-library export when needed.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import resource
import sys
import time

from tests.bench_svd_background import _backup_library, _saved_library_source
from tests.repos.test_private_analytics import OPERATIONS
from yt_playlist.core.activity import work_budget
from yt_playlist.core.store import Store


def test_actual_library_private_analytics_match_owned_connection_and_allow_writes(tmp_path):
    path = tmp_path / 'library.db'
    source = _saved_library_source()
    report = {'source': str(source), 'python': sys.version,
              'backup': _backup_library(source, path), 'queries': []}
    store = Store(path)
    store.init_schema()
    wal = Path(str(path) + '-wal')

    def wal_bytes():
        return wal.stat().st_size if wal.exists() else 0

    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    try:
        playlist = store.conn.execute('SELECT id FROM playlists ORDER BY id LIMIT 1').fetchone()
        artist = store.conn.execute('SELECT name FROM artists ORDER BY id LIMIT 1').fetchone()
        album = store.conn.execute("SELECT provider_id FROM album_provider_ids WHERE provider='youtube' LIMIT 1").fetchone()
        report['selected_targets'] = {'playlist': playlist[0] if playlist else None,
                                      'artist': artist[0] if artist else None,
                                      'album': album[0] if album else None}
        now = time.time()
        baseline = {}
        operations = []
        for repo_name, method, args in OPERATIONS:
            values = tuple(playlist[0] if value == 'playlist' and playlist else
                           artist[0] if value == 'Artist' and artist else
                           album[0] if value == 'B' and album else value for value in args)
            if method in ('ledger_track_plays', 'ledger_artist_plays', 'play_hours'):
                values = (0, now)
            repo = getattr(store, repo_name)
            target = getattr(repo, method)
            started = time.monotonic()
            # Reproduce the old connection/lock boundary without changing SQL.
            # This baseline is serial and confined to the disposable copy.
            with work_budget(10), store._lock:
                value = target.__wrapped__(repo, *values)
            elapsed = time.monotonic() - started
            label = f'{repo_name}.{method}'
            baseline[label] = digest(value)
            report['queries'].append({'method': label, 'owned_connection_s': elapsed,
                                      'rows': len(value), 'sha256': baseline[label]})
            operations.append((repo_name, method, values))
            del value

        reads, peak_wal = [], wal_bytes()

        def calculate():
            for (repo_name, method, values), row in zip(operations, report['queries']):
                started, cpu = time.monotonic(), time.thread_time()
                value = getattr(getattr(store, repo_name), method)(*values)
                row.update(private_s=time.monotonic() - started, cpu_s=time.thread_time() - cpu)
                assert digest(value) == baseline[row['method']], row

        with ThreadPoolExecutor(1) as workers:
            pending = workers.submit(calculate)
            while not pending.done():
                started = time.monotonic()
                store.settings.set_setting('analytics_contention_probe', str(len(reads)))
                store.settings.get_setting('analytics_contention_probe')
                reads.append(time.monotonic() - started)
                peak_wal = max(peak_wal, wal_bytes())
                time.sleep(.01)
            pending.result()
        ordered = sorted(reads)
        report.update(foreground_samples=len(reads), wal_peak_bytes=peak_wal, wal_end_bytes=wal_bytes(),
            foreground_p95_ms=ordered[min(len(ordered) - 1, int(len(ordered) * .95))] * 1000 if reads else None,
            foreground_p99_ms=ordered[min(len(ordered) - 1, int(len(ordered) * .99))] * 1000 if reads else None,
            foreground_max_ms=max(reads, default=0) * 1000,
            peak_process_rss_mib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (
                1 if sys.platform == 'darwin' else 1024) / 1024 ** 2)
        assert reads, 'No overlapping foreground probe was observed'
        assert report['foreground_p95_ms'] < 100, report
        assert report['foreground_p99_ms'] < 250, report
        assert report['foreground_max_ms'] < 1000, report
    finally:
        destination = tmp_path / 'private-analytics.json'
        destination.write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps({'report': str(destination), **report}), flush=True)
        store.conn.close()
