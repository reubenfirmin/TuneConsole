"""Opt-in fresh-process maintenance/logging probe on an isolated fixture library.

uv run pytest --test-lock-wait=60 -s tests/bench_maintenance_logging.py
The parent holds the shared test lock throughout the 61-second child process.
No live library, provider requests or playback are involved.
"""
from collections import Counter
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time

from yt_playlist.core.numerics import limit_native_threads
from yt_playlist.core.store import Store
from yt_playlist.core.work_diagnostics import work_span
from yt_playlist.rec import home_prepared, model_refresh, mode_surfaces, rec_params


def measure(directory):
    limit_native_threads()
    store = Store(directory / 'library.db')
    store.init_schema()
    identity = store.identities.upsert_identity('Logging probe', 'fixture', None, True)
    tracks = []
    # Mode discovery requires at least 80 content vectors. Include enough real
    # library members to exercise populated bundles rather than the cold path.
    for i in range(96):
        tid = store.tracks.upsert_track(f'probe-{i}', f'Song {i}', f'Artist {i // 4}', None, None)
        store.tracks.set_track_genre(tid, ('Techno', 'Jazz', 'Rock')[i // 32])
        tracks.append(tid)
    for i in range(12):
        pid = store.playlists.upsert_playlist(identity, f'playlist-{i}', f'Playlist {i}', 16, '', 1)
        store.playlists.set_playlist_tracks(pid, (tracks + tracks)[i * 8:i * 8 + 16])
    store.settings.set_setting('rec_dim', '16')
    rec_params.set_param(store, 'modes_k', 3)
    rec_params.set_param(store, 'modes_min_members', 8)
    store.rec_surface.put_proposals('home_prepared', {'last-good': True}, time.time())
    # Exercise the real cooperative budget boundary without a two-second wait
    # on each pass. The normal configured budget is unchanged in the application.
    home_prepared.PREPARE_BUDGET_S = 0
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    log = logging.getLogger('yt_playlist')
    previous_level = log.level
    log.setLevel(logging.DEBUG)
    handler = Capture()
    log.addHandler(handler)
    started = time.monotonic()
    start_utc = datetime.now(timezone.utc).isoformat()
    store.maintenance_diagnostics.started = started
    fits = bundles = guarded = budget = 0
    try:
        attempt = 0
        while time.monotonic() - started < 61:
            now = time.time()
            store.tracks.set_track_year(tracks[0], str(1980 + attempt))
            with work_span('rec: recommendation models', announce=True, level=logging.DEBUG):
                prepared = model_refresh.rebuild(store, now)
                assert prepared is not None and 'rec_content_vectors' in prepared['vectors']
                fits += 1
            with work_span('rec: cluster surfaces and PPR', announce=True, level=logging.DEBUG):
                assert mode_surfaces.prepare_bundles(store, now) is not None
                bundles += 1
                with store.read_snapshot() as snapshot:
                    snapshot.store.settings.get_setting('probe')
                    store.settings.set_setting('probe', str(attempt))
                    assert not snapshot.publish(lambda: None)
                    guarded += 1
            with work_span('rec: Home collections', announce=True, level=logging.DEBUG):
                assert home_prepared.prepare(store, now) is None
                budget += 1
            store.maintenance_diagnostics.flush()
            attempt += 1
            time.sleep(max(0, min(started + 61, started + attempt * 2) - time.monotonic()))
        store.maintenance_diagnostics.flush()
        elapsed = time.monotonic() - started
        info = [r for r in records if r.levelno >= logging.INFO]
        summaries = [r for r in info if r.msg.startswith('Maintenance summary:')]
        (directory / 'maintenance-debug.log').write_text('\n'.join(
            f'{datetime.fromtimestamp(r.created, timezone.utc).isoformat()} '
            f'{r.levelname} {r.name} {r.getMessage()}' for r in records) + '\n')
        (directory / 'maintenance-info.log').write_text('\n'.join(
            f'{datetime.fromtimestamp(r.created, timezone.utc).isoformat()} '
            f'{r.levelname} {r.name} {r.getMessage()}' for r in info) + '\n')
        prepared_bundles = store.rec_surface.get_proposals('mode_bundles') or {}
        mode_bundles = [bucket for mode, bucket in prepared_bundles.items() if mode.isdigit()]
        bundled_tracks = {item['key'] for bucket in mode_bundles
                          for items in bucket.values() for item in items}
        return {'start_utc': start_utc, 'end_utc': datetime.now(timezone.utc).isoformat(),
                'elapsed_s': elapsed, 'levels': dict(Counter(r.levelname for r in records)),
                'summary_count': len(summaries), 'summaries': [r.getMessage() for r in summaries],
                'model_publications': fits, 'bundle_publications': bundles,
                'source_deferrals': guarded, 'home_budget_deferrals': budget,
                'modeled_tracks': store.rec_model.rec_vectors_count(),
                'prepared_bundle_modes': len(mode_bundles),
                'prepared_bundle_tracks': len(bundled_tracks),
                'last_good_home': store.rec_surface.get_proposals('home_prepared')}
    finally:
        log.removeHandler(handler)
        log.setLevel(previous_level)
        store.conn.close()


def test_fresh_process_live_log_window(tmp_path):
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), str(tmp_path)],
                           capture_output=True, text=True, check=True, timeout=90)
    result = json.loads(child.stdout)
    report = Path(os.environ.get('TUNECONSOLE_LOG_REPORT', str(tmp_path / 'maintenance.json')))
    report.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'report': str(report), 'logs': str(tmp_path), **result}), flush=True)
    assert result['elapsed_s'] >= 61
    assert result['summary_count'] == 1
    assert result['levels'].get('INFO') == 1
    assert result['model_publications'] >= 20
    assert result['bundle_publications'] == result['model_publications']
    assert result['source_deferrals'] == result['home_budget_deferrals'] == result['bundle_publications']
    assert result['modeled_tracks'] == 96
    assert result['prepared_bundle_modes'] > 0
    assert result['prepared_bundle_tracks'] > 0
    assert result['last_good_home'] == {'last-good': True}


if __name__ == '__main__':
    print(json.dumps(measure(Path(sys.argv[1]))))
