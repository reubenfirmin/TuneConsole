"""Compare model history and Vibe fit data across a private upgrade."""
from contextlib import closing
import json
import sqlite3
import struct

import pytest

from tests.library_image.fixtures import managed_database, report_file
from yt_playlist.core import acousticbrainz_projection as estimates
from yt_playlist.core.acousticbrainz_vibe_schema import LINKED_CLASSIFIERS
from yt_playlist.core.audio_vibe_contract import score_version_filter, served_score
from yt_playlist.core.migrations.model_artifacts_storage import RETIRED
from yt_playlist.core.store import Store


def rows(conn, sql, args=()):
    return list(map(tuple, conn.execute(sql, args)))


def tables(conn):
    return {name for name, in conn.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def seed_restore_run(conn):
    keys = [key for key, in conn.execute('SELECT identity_key FROM recordings WHERE redirect_recording_id IS NULL '
                                         'ORDER BY identity_key LIMIT 2')]
    vectors = [(key, struct.pack('=2f', position + 0.25, -position - 0.5)) for position, key in enumerate(keys)]
    result = {'status': 'applied', 'previous': {'method': 'svd', 'dim': 2}, 'selected': {'method': 'svd', 'dim': 4}}
    run, = conn.execute('INSERT INTO rec_model_selection_runs(started_at,result_json,identity_version) '
                        'VALUES(100,?,2) RETURNING id', (json.dumps(result),)).fetchone()
    forecasts = [(100.00000000000001, '{"eligible": ["first"], "previous": [], "selected": ["first"]}'),
                 (125.25, '{"eligible":["second"],"previous":["second"],"selected":[]}')]
    conn.executemany('INSERT INTO rec_model_forecasts VALUES (?,?,?)', [(run, *item) for item in reversed(forecasts)])
    conn.executemany('INSERT INTO rec_model_previous_vectors VALUES (?,?,?)', [(run, *item) for item in vectors])
    conn.commit()
    return run, result, forecasts, vectors


def test_installed_model_artifacts_and_estimates():
    report = {}
    path = managed_database(report=report)
    if path is None:
        pytest.skip('Requires a private managed C175 snapshot')
    selected_query = 'SELECT * FROM recording_vibes ORDER BY recording_id,vibe'
    served_query = 'SELECT * FROM served_vibe_fits ORDER BY recording_id,vibe,source,prediction_id'
    patterns = ('*', '*', '*')
    with closing(sqlite3.connect(path)) as old:
        before = tables(old)
        installed = {name: old.execute('SELECT count(*) FROM ' + name).fetchone()[0] for name in RETIRED}
        run, result, forecasts, vectors = seed_restore_run(old)
        history = rows(old, 'SELECT id,started_at,result_json,identity_version FROM rec_model_selection_runs ORDER BY id')
        selected, served = rows(old, selected_query), rows(old, served_query)
        estimates_before = rows(old, 'SELECT * FROM acousticbrainz_vibe_estimates ORDER BY recording_id,vibe')
        page = rows(old, 'WITH page AS (SELECT DISTINCT recording_id FROM acousticbrainz_vibe_estimates '
                    'WHERE recording_id>? ORDER BY recording_id LIMIT ?) '
                    'SELECT page.recording_id,i.classifiers,e.vibe,e.inputs_version FROM page '
                    f'JOIN ({LINKED_CLASSIFIERS}) i USING(recording_id) JOIN acousticbrainz_vibe_estimates e USING(recording_id) '
                    'ORDER BY page.recording_id,e.vibe', (0, 64))
        keys = [key for key, in old.execute('SELECT identity_key FROM recordings r '
                    'WHERE musical_profile_version IS NOT NULL AND EXISTS '
                    '(SELECT 1 FROM acousticbrainz_vibe_estimates e WHERE e.recording_id=r.id) ORDER BY id LIMIT 128')]
        marks = ','.join('?' for _ in keys)
        bulk = {vibe: dict(old.execute(f'SELECT r.identity_key,{served_score("fit")} FROM served_vibe_fits fit '
                    f'JOIN recordings r ON r.id=fit.recording_id WHERE r.identity_key IN ({marks}) '
                    f'AND fit.vibe=? AND {score_version_filter("fit.")} GROUP BY r.identity_key', (*keys, vibe, *patterns)))
                for vibe in estimates.VIBES}
    store = Store(path)
    with closing(store.conn) as conn:
        store.init_schema()
        after = tables(conn)
        assert set(RETIRED) <= before - after and not after - before
        assert rows(conn, 'SELECT id,started_at,result_json,identity_version FROM rec_model_selection_runs ORDER BY id') == history
        assert rows(conn, selected_query) == selected
        assert rows(conn, served_query) == served
        assert rows(conn, 'SELECT r.id,e.key,e.value->>0,e.value->>1 FROM recordings r '
                    'JOIN json_each(r.acousticbrainz_estimates) e ORDER BY r.id,e.key') == estimates_before
        assert list(map(tuple, store.vibe_predictions.acousticbrainz_estimate_page(0, 64))) == page
        assert {vibe: store.vibe_predictions.repository.fit_scores(keys, vibe, patterns=patterns)
                for vibe in estimates.VIBES} == bulk
        assert not conn.execute('PRAGMA foreign_key_check').fetchone()
    store = Store(path)
    with closing(store.conn) as conn:
        store.init_schema()
        assert store.model_selection.selection_run(run) == {'id': run, **result}
        assert json.loads(conn.execute('SELECT forecast_series FROM rec_model_selection_runs WHERE id=?', (run,)).fetchone()[0]) == [
            list(item) for item in forecasts]
        assert store.model_selection.model_forecasts(run) == [{'made_at': at, **json.loads(data)} for at, data in forecasts]
        assert store.model_selection.restore_model_selection(run, 130)
        assert sorted(store.rec_model.get_rec_vectors()) == vectors
        assert store.model_selection.selection_run(run)['status'] == 'restored'
    report.update(tables_before=len(before), tables_after=len(after), installed_rows=installed,
                  exact_historical_runs=len(history) - 1, seeded_forecasts=len(forecasts), restored_vectors=len(vectors),
                  exact_estimates=len(estimates_before), exact_selected_fits=len(selected), exact_served_fits=len(served),
                  estimate_page_entries=len(page), bulk_recordings=len(keys), bulk_presets=len(bulk), foreign_key_errors=0)
    report_file('model-artifacts-c189.json').write_text(json.dumps(report, indent=2) + '\n')
