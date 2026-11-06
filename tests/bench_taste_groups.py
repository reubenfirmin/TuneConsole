"""Opt-in production preparation budget and lineage acceptance:

uv run pytest -s tests/bench_taste_groups.py

Optional TUNECONSOLE_GROUP_READONLY_DB freezes only prepared library/profile inputs
from mode=ro/query_only SQLite. TUNECONSOLE_GROUP_SNAPSHOT replays that JSON file;
TUNECONSOLE_GROUP_REPORT selects the local output JSON. No migrations or provider calls.
"""
import hashlib
import json
import time
import pytest

from tests.group_benchmark_support import frozen_inputs, report_path
from yt_playlist.core import prepared_inputs
from yt_playlist.rec import group_discovery, group_preparation


def test_production_preparation_and_update_budgets_retain_lineage(tmp_path, store, request, monkeypatch):
    frozen = frozen_inputs(request, tmp_path)
    if frozen is None:
        pytest.skip('Supply --group-snapshot or --group-readonly-db for production budget acceptance')
    reports = {}
    output = report_path(request, tmp_path, 'group-production-budgets.json')
    if frozen:
        # Exercise the actual paged worker and its default ten-second/512-node
        # limits on a disposable database, never the captured user's database.
        with store.conn:
            for i, (key, payload, _profile) in enumerate(frozen['rows']):
                store.conn.execute('INSERT INTO recordings(identity_key) VALUES (?) '
                    'ON CONFLICT(identity_key) DO NOTHING', (key,))
                prepared_inputs.upsert(store.conn, key, payload, 'f' * 64, 1, i + 1)
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
        key, payload, _profile = next(row for row in frozen['rows'] if row[1].get('library') is not None)
        library = payload['library']
        payload = {**payload, 'library': {**library, 'year': '2001' if library.get('year') != '2001' else '2002'}}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with store.conn:
            prepared_inputs.upsert(store.conn, key, payload, digest, 2_000_000_059, len(frozen['rows']) + 1)
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
