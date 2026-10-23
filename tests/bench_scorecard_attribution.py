"""Opt-in serial #261 workload; every database and measurement is disposable.

Run explicitly through pytest when authorized. This file is excluded from normal
test discovery. No live database, network service or music metadata is needed.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import sqlite3
import statistics
import threading
import time

import pytest

from yt_playlist.core import db_diagnostics
from yt_playlist.core.activity import cancellable_work
from yt_playlist.core.store import Store
from yt_playlist.repos.scorecard_attribution import VIDEO


START = 1_800_000_000
DAY = 86400
ITEMS = 3000
IMPRESSIONS = 20000
FOREGROUND_BUDGET_SECONDS = 1.0


def _seed(store, exit_count, exit_videos):
    """Model repeated opportunities across nine active days in a 30-day window."""
    iid = store.identities.upsert_identity('profile', 'fixture', None, True)
    impressions, plays, responses = [], [], []
    times = []
    for index in range(IMPRESSIONS):
        item = index % ITEMS
        # Nine active days spread across the window, with repeated views per item.
        active_day, within_day = divmod(index, math.ceil(IMPRESSIONS / 9))
        at = START + active_day * 3 * DAY + within_day * 30
        times.append(at)
        provenance = json.dumps({
            'policy_kind': 'live', 'ranker': ('cosine', 'ppr')[index % 2],
            'candidate': {
                'video_id': f'video-{item}', 'artist': f'artist-{item % 300}',
                'genre': f'genre-{item % 20}',
                'ownership': 'cold' if item % 3 == 0 else 'owned',
                'last_played': None if item % 4 == 0 else at - (item % 100 + 1) * DAY,
                'coverage': {'genre': True, 'year': item % 3 != 0, 'audio': item % 2 == 0},
            },
            'experiment': {'comparison_id': f'opportunity-{index // 20}',
                           'cohort': f'cohort-{index % 2}',
                           'cluster': f'day-{active_day}-mode-{item % 4}'},
        }, sort_keys=True)
        impressions.append((f'request-{index // 20}', ('home', 'mode')[index % 2],
                            f'lane-{item % 3}', item % 4, f'item-{item}', index % 20 + 1,
                            .5, 'profile-model', provenance, at))
        if index % 5 == 0:
            plays.append((iid, f'item-{item}', f'video-{item}', at + 90,
                          (0, 1, None)[(index // 5) % 3], 'fixture'))
        if index % 20 == 0:
            responses.append((f'response-{index}', ('open', 'start', 'save', 'promotion')[(index // 20) % 4],
                              f'item-{item}', at + 30, (index // 20) % 2))

    exits = []
    for index in range(exit_count):
        item = index % exit_videos
        opportunity = item + ((index // exit_videos + item) % 7) * ITEMS
        exits.append((iid, ('track_exit', 'bye', 'ended')[index % 3], f'video-{item}',
                      5 if index % 4 == 0 else 195, 200, times[opportunity] + 210))
    with store.conn:
        store.conn.executemany(
            'INSERT INTO rec_served_impressions(request_id,surface,lane,mode_id,identity_key,'
            'rank,score,model_version,provenance,served_at) VALUES (?,?,?,?,?,?,?,?,?,?)', impressions)
        store.conn.executemany(
            'INSERT INTO play_events(identity_id,identity_key,video_id,played_at,is_generated,source) '
            'VALUES (?,?,?,?,?,?)', plays)
        store.conn.executemany(
            'INSERT INTO rec_response_events(event_id,kind,identity_key,at,is_generated) VALUES (?,?,?,?,?)',
            responses)
        store.conn.executemany(
            'INSERT INTO player_events(identity_id,kind,video_id,position,duration,at) VALUES (?,?,?,?,?,?)',
            exits)
        store.conn.execute('ANALYZE')
    store.settings.set_setting('scorecard_bench_probe', '0')
    return {'window_days': 30, 'active_impression_days': 9, 'impressions': len(impressions),
            'distinct_items': ITEMS, 'plays': len(plays), 'responses': len(responses),
            'exits': len(exits), 'distinct_exit_videos': exit_videos}


def _latencies(samples, operation):
    values = sorted(sample[operation] for sample in samples)
    if not values:
        return {'count': 0}
    return {'count': len(values), 'median': statistics.median(values),
            'p95': values[math.ceil(len(values) * .95) - 1], 'max': values[-1]}


@pytest.mark.parametrize('profile,exit_count,exit_videos', [
    ('current_volume', 300, 225),
    ('exit_stress', 6000, 1200),
])
def test_scorecard_profile(tmp_path, monkeypatch, record_property, profile, exit_count, exit_videos):
    store = Store(tmp_path / 'scorecard-profile.db')
    entered = threading.Event()
    stop = threading.Event()
    lookups, foreground = [], []
    report = {'profile': profile, 'sqlite': sqlite3.sqlite_version,
              'foreground_budget_seconds': FOREGROUND_BUDGET_SECONDS,
              'video_lookups': lookups, 'foreground_samples': foreground}
    original = db_diagnostics.DiagnosticCursor._call

    def measured(cursor, stage, target, *args):
        if stage == 'execute' and 'WHERE identity_key IN (' in cursor._sql:
            entered.set()
        result = original(cursor, stage, target, *args)
        if stage == 'execute' and f'WHERE {VIDEO} IN (' in cursor._sql:
            # Explain the exact executed batch, preserving its IN cardinality,
            # partial-index predicate and horizon bounds. Never explain a shortcut.
            with_plan = sqlite3.Cursor(cursor.connection)
            try:
                plan = with_plan.execute('EXPLAIN QUERY PLAN ' + args[0], args[1]).fetchall()
            finally:
                with_plan.close()
            lookups.append({'sql_id': hashlib.sha256(args[0].encode()).hexdigest()[:12],
                            'video_parameters': len(args[1]) - 2,
                            'serving_connection': cursor.connection is store.conn,
                            'plan': [row[3] for row in plan]})
        return result

    def run_report():
        started = time.monotonic()
        try:
            with cancellable_work(lambda: stop.is_set() or time.monotonic() - started > 120):
                return store.rec_surface.recommendation_scorecard(START, START + 30 * DAY)
        finally:
            report['scorecard_seconds'] = time.monotonic() - started

    try:
        store.init_schema()
        report['counts'] = _seed(store, exit_count, exit_videos)
        monkeypatch.setattr(db_diagnostics.DiagnosticCursor, '_call', measured)
        # The only concurrent work is the report and the foreground probe under
        # measurement. No attribution pause or injected report delay is used.
        with ThreadPoolExecutor(1) as worker:
            future = worker.submit(run_report)
            try:
                assert entered.wait(10), 'Real attribution queries did not start'
                for index in range(32):
                    if future.done():
                        break
                    sample = {'started_during_report': True}
                    started = time.monotonic()
                    assert store.settings.get_setting('scorecard_bench_probe') == str(index)
                    sample['read_seconds'] = time.monotonic() - started
                    started = time.monotonic()
                    store.settings.set_setting('scorecard_bench_probe', str(index + 1))
                    sample['write_seconds'] = time.monotonic() - started
                    sample['finished_during_report'] = not future.done()
                    foreground.append(sample)
                    time.sleep(.005)  # Pace foreground traffic, without delaying the report.
                result = future.result(120)
            finally:
                stop.set()

        report['foreground_reads'] = _latencies(foreground, 'read_seconds')
        report['foreground_writes'] = _latencies(foreground, 'write_seconds')
        report['fully_overlapping_samples'] = sum(s['finished_during_report'] for s in foreground)
        report['result'] = {key: result[key] for key in
                            ('impressions', 'unique_items', 'played', 'organic', 'generated',
                             'unknown_provenance', 'skip', 'completion', 'open', 'start', 'save', 'promotion')}
        report['result_digest'] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
        assert result['impressions'] == IMPRESSIONS
        assert result['unique_items'] == ITEMS
        assert result['played'] > 0 and result['skip'] > 0 and result['completion'] > 0
        assert report['fully_overlapping_samples'] > 0, 'Foreground/report overlap was not measured'
        assert report['foreground_reads']['max'] < FOREGROUND_BUDGET_SECONDS
        assert report['foreground_writes']['max'] < FOREGROUND_BUDGET_SECONDS
        assert len(lookups) == math.ceil(exit_videos / 400)
        for lookup in lookups:
            assert 0 < lookup['video_parameters'] <= 400
            assert not lookup['serving_connection']
            assert any('SEARCH' in row and 'ix_rec_served_video_at' in row for row in lookup['plan'])
    finally:
        stop.set()
        store.conn.close()
        output = tmp_path / f'scorecard-{profile}.json'
        output.write_text(json.dumps(report, indent=2) + '\n')
        record_property('scorecard_profile', json.dumps(report, sort_keys=True))
        print(json.dumps({'report': str(output), **report}), flush=True)
