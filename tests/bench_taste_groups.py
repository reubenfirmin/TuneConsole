"""Run under the normal pytest machine lock:

uv run pytest -s tests/bench_taste_groups.py

Optional TUNECONSOLE_GROUP_READONLY_DB freezes only prepared library/profile inputs
from mode=ro/query_only SQLite. TUNECONSOLE_GROUP_SNAPSHOT replays that JSON file;
TUNECONSOLE_GROUP_REPORT selects the local output JSON. No migrations or provider calls.
"""
import json
import os
from pathlib import Path
import sqlite3
import time

from tests.group_support import recordings
from yt_playlist.rec import group_discovery, group_eval, group_preparation, group_similarity


def test_compare_adaptive_engines_on_independently_prepared_libraries(tmp_path, store, request, monkeypatch):
    cases = {
        'small-album': recordings([('Pop', 8, .25)]),
        'metal-plus-pop': recordings([('Death Metal', 180, .9), ('Thrash Metal', 60, .8), ('Pop', 12, .25)]),
        'eclectic': recordings([('Death Metal', 45, .9), ('House', 45, .7), ('Ambient', 45, .1),
                               ('Jazz', 45, .4), ('Pop', 12, .3)]),
        'same-style-two-sounds': recordings([('House', 45, .1), ('House', 45, .95)]),
    }
    snapshot_path = request.config.getoption('group_snapshot') or os.getenv('TUNECONSOLE_GROUP_SNAPSHOT')
    database = request.config.getoption('group_readonly_db') or os.getenv('TUNECONSOLE_GROUP_READONLY_DB')
    if database:
        with sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True) as conn:
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
            rows = conn.execute('SELECT p.identity_key,p.payload,m.payload FROM rec_prepared_content p '
                                'LEFT JOIN rec_musical_profiles m USING(identity_key) ORDER BY p.identity_key').fetchall()
        frozen = {'rows': [[key, json.loads(raw), json.loads(profile) if profile else None] for key, raw, profile in rows]}
        snapshot_path = snapshot_path or str(tmp_path / 'musical-group-inputs.json')
        Path(snapshot_path).write_text(json.dumps(frozen))
    if snapshot_path:
        frozen = json.loads(Path(snapshot_path).read_text())
        cases['frozen-library'] = [group_similarity.recording(key, payload['library'], profile)
            for key, payload, profile in frozen['rows'] if payload['library'] is not None]
    reports = {}
    for name, tracks in cases.items():
        reports[name] = group_eval.compare(tracks, policy=group_discovery.Policy(max_groups=512))
        print(json.dumps({'case': name, 'n': len(tracks), 'engines': {engine: {
            **{key: row.get(key) for key in ('leaves', 'assigned', 'unknown', 'edges', 'named_leaves', 'seconds', 'growth_ari')},
            'retained_small_styles': sum(row['minority_style_retention'].values()),
            'small_styles': len(row['minority_style_retention']), 'largest': max(row['sizes'], default=0)}
            for engine, row in reports[name]['engines'].items()}}), flush=True)
        assert all(row['assigned'] + row['unknown'] == len(tracks) for row in reports[name]['engines'].values())
    output = Path(request.config.getoption('group_report') or os.getenv('TUNECONSOLE_GROUP_REPORT')
                  or tmp_path / 'group-comparison.json')
    output.write_text(json.dumps(reports, indent=2))
    print('Report:', output)
    if snapshot_path:
        reports['feature-comparison'] = group_eval.feature_comparison(cases['frozen-library'])
        print(json.dumps({'feature-comparison': {name: {key: row.get(key) for key in ('leaves', 'assigned', 'unknown', 'seconds', 'deferred')}
              for name, row in reports['feature-comparison'].items()}}), flush=True)
        output.write_text(json.dumps(reports, indent=2))
        # Exercise the actual paged worker and its default ten-second/512-node
        # limits on a disposable database, never the captured user's database.
        with store.conn:
            store.conn.executemany('INSERT INTO rec_prepared_content VALUES (?,?,?,?,?)',
                [(key, json.dumps(payload), 'frozen', 1, i + 1) for i, (key, payload, _profile) in enumerate(frozen['rows'])])
            store.conn.executemany('INSERT INTO rec_musical_profiles VALUES (?,?,?,?,?)',
                [(key, json.dumps(profile), profile['version'], 'frozen', 1)
                 for key, _payload, profile in frozen['rows'] if profile])
            store.conn.execute("INSERT INTO rec_content_pending(identity_key) VALUES ('not-captured-yet')")
            store.conn.execute("INSERT INTO rec_musical_sources VALUES ('unfinished-source','test','artist','artist','{}',1)")
        original = group_discovery.discover

        def continuing_metadata(*args, **kwargs):
            result = original(*args, **kwargs)
            with store.conn:
                store.conn.execute("UPDATE rec_musical_sources SET revision=revision+1 WHERE source_key='unfinished-source'")
            return result

        monkeypatch.setattr(group_discovery, 'discover', continuing_metadata)
        worker = group_preparation.Preparation(store)
        started = time.monotonic()
        published = worker.step(2_000_000_000)
        head = store.taste_groups.head()
        reports['production-budget'] = {'published': published, 'seconds': time.monotonic() - started,
            'budget': vars(worker.budget), 'counts': head['payload']['counts'] if head else None,
            'progress': store.taste_groups.preparation_progress()}
        output.write_text(json.dumps(reports, indent=2))
        print(json.dumps({'production-budget': reports['production-budget']}), flush=True)
        assert published, 'The default worker must publish this admitted frozen library within its budgets'
        # Dynamic updates also need to fit after the first large generation;
        # identity/lineage reconciliation must not turn subsequent passes into permanent deferrals.
        worker.request_rebuild()
        started = time.monotonic()
        updated = worker.step(2_000_000_060)
        current = store.taste_groups.head()
        reports['update-budget'] = {'published': updated, 'seconds': time.monotonic() - started,
            'same_group_ids': {g['group_id'] for g in head['groups']} == {g['group_id'] for g in current['groups']},
            'historical_definitions_retained': len(store.taste_groups.definitions(head['generation'])) == len(head['groups'])}
        output.write_text(json.dumps(reports, indent=2))
        print(json.dumps({'update-budget': reports['update-budget']}), flush=True)
        assert updated and reports['update-budget']['same_group_ids']
        assert reports['update-budget']['historical_definitions_retained']
