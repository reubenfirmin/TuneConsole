"""Opt-in disposable continual-enrichment probe.

uv run pytest --test-lock-wait=60 -s tests/bench_progressive_preparation.py
Only a generated temporary database is written. Worker admission time is advanced
between slices; wall/CPU measurements cover actual preparation, not idle intervals.
"""
import json
import resource
import sys
import threading
import time

from yt_playlist.core.activity import check_stopping
from yt_playlist.core.db_diagnostics import PublicationTimings
from yt_playlist.core.numerics import limit_native_threads
from yt_playlist.core.store import Store
from yt_playlist.rec import model_refresh, rec_params
from yt_playlist.rec.progressive import Budget, Preparation


def test_continual_writes_publish_thin_then_richer_catalog(tmp_path, monkeypatch):
    limit_native_threads()
    store = Store(tmp_path / 'progressive.db')
    store.init_schema()
    store.conn.execute('PRAGMA journal_mode=WAL')
    n = 1536
    with store.conn:
        for i in range(n):
            key, video = f'song {i:04}|artist {i}', f'v{i}'
            store.conn.execute('INSERT INTO recordings(id,identity_key,title,artist,genre,bpm,energy) '
                               'VALUES (?,?,?,?,?,?,?)',
                               (i + 1, key, f'Song {i}', f'Artist {i}',
                                ('Techno', 'Jazz', 'Rock')[i % 3] if i % 4 == 0 else None,
                                80 + i % 100, (i % 10) / 10))
            store.conn.execute('INSERT INTO youtube_items(video_id,recording_id,title,artist,available,duration_s) '
                               'VALUES (?,?,?,?,1,200)', (video, i + 1, f'Song {i}', f'Artist {i}'))
            store.conn.execute('INSERT INTO catalog_entries(id,recording_id,video_id) VALUES (?,?,?)',
                               (i + 1, i + 1, video))
            store.conn.execute("INSERT INTO library_memberships(entry_id,origin) VALUES (?,'fixture')", (i + 1,))
    iid = store.identities.upsert_identity('Listener', 'fixture', None, True)
    for i in range(n // 48):
        pid = store.playlists.upsert_playlist(iid, f'playlist-{i}', f'Playlist {i}', 64, '', 1)
        store.playlists.set_playlist_tracks(pid, [(i * 48 + j) % n + 1 for j in range(64)])
    rec_params.set_param(store, 'modes_min_members', 8)
    rec_params.set_param(store, 'modes_k', 3)
    p = Preparation(store, budget=Budget())
    stop, started = threading.Event(), threading.Event()
    errors, latencies, publications, writes = [], [], [], []
    report = PublicationTimings.report
    def record(timing, outcome):
        publications.append({'outcome': outcome, 'stages': timing.stages})
        report(timing, outcome)
    monkeypatch.setattr(PublicationTimings, 'report', record)

    def enrich():
        try:
            i = 0
            while not stop.is_set():
                before = time.monotonic()
                with store._lock, store.conn:
                    # Two hot recordings never settle. Other recordings can be
                    # accepted independently, and basis fits read accepted inputs.
                    store.conn.execute('UPDATE recordings SET bpm=? WHERE id=?',
                                       (90 + i % 100, n - i % 2))
                    assert store.conn.execute('SELECT COUNT(*) FROM tracks').fetchone()[0] == n
                latencies.append(time.monotonic() - before)
                writes.append(i)
                started.set()
                i += 1
                stop.wait(.005)
        except BaseException as exc:
            errors.append(exc)
            started.set()
    thread = threading.Thread(target=enrich)
    thread.start()
    assert started.wait(5)
    phases = []
    wall, cpu = time.monotonic(), time.process_time()
    now = 1000
    svd = {}
    try:
        with store.priority.background(check_stopping):
            for phase in ('thin', 'richer'):
                if phase == 'richer':
                    with store._lock, store.conn:
                        store.conn.execute("UPDATE recordings SET genre=CASE id%3 "
                                           "WHEN 0 THEN 'Techno' WHEN 1 THEN 'Jazz' ELSE 'Rock' END")
                before = len(publications)
                for _ in range(n // 16 + 10):
                    p.capture(now)
                    p.fit(now)
                    now += 5
                    if p.status()['prepared'] >= n - 2 and p.status()['pending'] <= 2:
                        break
                # Give a due basis one more admission after capture has drained.
                now += 60
                p.fit(now)
                phases.append({'phase': phase, **p.status(),
                               'publications': len(publications) - before})
        assert not errors
        assert all(row['prepared'] >= n - 2 for row in phases)
        assert phases[-1]['basis']['serial'] > phases[0]['basis']['serial']
        assert store.modes.list_modes(active_only=True)
        assert len(writes) > 10
        assert all(row['library_vectors'] >= n - 2 for row in phases)
        cache = model_refresh.FitCache()
        before, fit_wall, fit_cpu = len(writes), time.monotonic(), time.process_time()
        with store.priority.background(check_stopping):
            result = model_refresh.rebuild(store, now, fit_cache=cache,
                                           memory_bytes=p.budget.full_memory_bytes)
        svd = {'deferred_during_writes': result is None, 'concurrent_writes': len(writes) - before,
               'wall_s': time.monotonic() - fit_wall, 'cpu_s': time.process_time() - fit_cpu,
               'retained_fit_bytes': sum(cache.sizes.values())}
        assert result is None and svd['concurrent_writes'] > 0
        assert any(kind == 'tracks' and rows for (kind, _), rows in cache.entries.items())
        stop.set()
        thread.join(5)
        assert not thread.is_alive()
        assert model_refresh.rebuild(store, now + 1, fit_cache=cache,
                                     memory_bytes=p.budget.full_memory_bytes)
        svd['published_vectors'] = store.rec_model.rec_vectors_count()
        assert svd['published_vectors'] == n
    finally:
        stop.set()
        thread.join(5)
        store.conn.close()
    scale = 1 if sys.platform == 'darwin' else 1024
    result = {'rows': n, 'continual_writes': len(writes), 'wall_s': time.monotonic() - wall,
              'cpu_s': time.process_time() - cpu,
              'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale,
              'foreground_database_max_s': max(latencies, default=0),
              'publication_lock_max_s': max((r['stages'].get('lock', {}).get('wall_s', 0)
                                             for r in publications), default=0),
              'phases': phases, 'svd': svd}
    artifact = tmp_path / 'progressive-results.json'
    artifact.write_text(json.dumps(result, indent=2))
    print(json.dumps({'artifact': str(artifact), **result}, indent=2))
