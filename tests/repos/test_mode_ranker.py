import sqlite3

import pytest
from yt_playlist.core.store import Store


@pytest.fixture
def store():
    s = Store(":memory:")
    s.init_schema()
    return s


def test_migration_adds_ranker_columns_to_old_db(tmp_path):
    # simulate a pre-#57 DB without the ranker columns
    db = tmp_path / "old.db"
    raw = sqlite3.connect(db)
    raw.execute("CREATE TABLE rec_mode_impressions (epoch INTEGER NOT NULL, lane TEXT NOT NULL, "
                "mode_id INTEGER NOT NULL, created_at REAL NOT NULL, PRIMARY KEY (epoch, lane))")
    raw.execute("CREATE TABLE rec_mode_picks (playlist_id INTEGER PRIMARY KEY, "
                "mode_id INTEGER NOT NULL, created_at REAL NOT NULL)")
    raw.commit()
    raw.close()
    s = Store(str(db))
    s.init_schema()
    icols = {r["name"] for r in s.conn.execute("PRAGMA table_info(rec_mode_impressions)")}
    pcols = {r["name"] for r in s.conn.execute("PRAGMA table_info(rec_mode_picks)")}
    assert "ranker" in icols
    assert "ranker" in pcols


def test_ranker_impression_counts_coalesces_null(store):
    store.modes.log_impressions(1, [("wheelhouse", 1, "ppr"), ("explore", 2, "cosine")], now=100.0)
    store.modes.log_impressions(2, [("wheelhouse", 1)], now=110.0)   # 2-tuple -> ranker NULL -> 'cosine'
    counts = store.modes.ranker_impression_counts()
    assert counts == {"ppr": 1, "cosine": 2}


def test_ranker_pick_rows_and_since(store):
    store.modes.log_pick(10, 1, 100.0, ranker="ppr")
    store.modes.log_pick(11, 2, 200.0)                               # ranker NULL -> 'cosine'
    assert sorted(store.modes.ranker_pick_rows()) == [(10, "ppr"), (11, "cosine")]
    assert store.modes.ranker_pick_rows(since=150.0) == [(11, "cosine")]


def test_legacy_pick_rows_unchanged(store):
    # #57 must not disturb the 2-tuple pick_rows the Thompson sampler / scoreboard rely on.
    store.modes.log_pick(10, 1, 100.0, ranker="ppr")
    assert store.modes.pick_rows() == [(10, 1)]
