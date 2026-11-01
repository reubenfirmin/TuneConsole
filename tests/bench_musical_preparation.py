"""Opt-in recurring 96-recording musical preparation throughput budget.

uv run pytest tests/bench_musical_preparation.py -s
"""
import time

from tests.rec.test_musical_preparation import drain, observe
from yt_playlist.rec import musical_profiles as profiles


def test_profile_preparation_budget_on_synthetic_catalog(store, record_property):
    for i in range(96):
        tid = store.tracks.upsert_track(f'v{i}', f'Song {i}', 'Artist', '', 200)
        observe(store, tid, [{'name': 'Metal'}, {'name': 'Ambient'}])
    start = time.monotonic()
    drain(store)
    elapsed = time.monotonic() - start
    record_property('musical_profile_96_recordings_seconds', elapsed)
    assert elapsed < 15
    assert store.conn.execute('SELECT COUNT(*) FROM rec_musical_profiles').fetchone()[0] == 96
    plans = store.conn.execute('EXPLAIN QUERY PLAN SELECT identity_key FROM rec_musical_profiles '
                               'WHERE version<? LIMIT 32', (profiles.VERSION,)).fetchall()
    assert any('SEARCH' in row[3] and 'version' in row[3] for row in plans)
