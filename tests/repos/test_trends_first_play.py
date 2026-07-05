import pytest
from yt_playlist.core.store import Store


@pytest.fixture
def store():
    s = Store(":memory:")
    s.init_schema()
    s.upsert_identity("me", "c", None, True)   # identity id=1, referenced by history/play_events FKs
    return s


def _snap(store, day, keys):
    """One history snapshot at taken_at = day*86400 containing `keys`. Returns snapshot id."""
    cur = store.conn.execute("INSERT INTO history_snapshots(identity_id, taken_at) VALUES (1, ?)",
                             (day * 86400.0,))
    sid = cur.lastrowid
    for k in keys:
        store.conn.execute("INSERT INTO history_items(snapshot_id, identity_key) VALUES (?, ?)", (sid, k))
    store.conn.commit()
    return sid


def _track(store, key, artist, genre=""):
    store.conn.execute(
        "INSERT INTO tracks(identity_key, video_id, title, artist, genre) VALUES (?,?,?,?,?)",
        (key, "v" + key, "T" + key, artist, genre))
    store.conn.commit()


def _build(store):
    """Mirror trend_rollups' incremental fold: history + play_events, then artist derivation."""
    hist = store.trends.history_track_first(0)
    plays = store.trends.play_event_track_first()
    rows = []
    for k in set(hist) | set(plays):
        cands = []
        if k in hist:
            cands.append((hist[k][1], hist[k][0], "history"))
        if k in plays:
            cands.append((plays[k][1], plays[k][0], "play_event"))
        ts, day, src = min(cands)
        rows.append(("track", k, day, ts, src))
    store.trends.upsert_first_play_min(rows)
    store.trends.rebuild_artist_first_play()


def test_first_play_min_over_history_and_play_events(store):
    for k, a in [("k1", "A1"), ("k2", "A1"), ("k3", "A2"), ("k4", "A3")]:
        _track(store, k, a)
    _snap(store, 2, ["k1", "k3"])          # day 2
    _snap(store, 5, ["k1", "k2"])          # day 5
    _snap(store, 9, ["k3", "k4"])          # day 9
    # play_events: k1 played day 1 (earlier than its day-2 snapshot); k4 played day 8 (earlier than day 9)
    store.conn.execute("INSERT INTO play_events(identity_id, identity_key, played_at) VALUES (1,'k1',?)",
                       (1 * 86400.0,))
    store.conn.execute("INSERT INTO play_events(identity_id, identity_key, played_at) VALUES (1,'k4',?)",
                       (8 * 86400.0,))
    store.conn.commit()
    _build(store)
    tracks = store.trends.first_play_map("track")
    # k1 = min(hist day2, play day1) = 1 ; k2 = day5 ; k3 = min(day2, day9) = 2 ; k4 = min(day9, play day8) = 8
    assert tracks == {"k1": 1, "k2": 5, "k3": 2, "k4": 8}
    artists = store.trends.first_play_map("artist")
    # A1 = min(k1=1, k2=5) = 1 ; A2 = k3 = 2 ; A3 = k4 = 8
    assert artists == {"A1": 1, "A2": 2, "A3": 8}
    # floor = earliest track evidence = day 1 (k1 via play_events)
    assert store.trends.first_play_floor_day() == 1


def test_source_records_winning_model(store):
    _track(store, "k1", "A1")
    _snap(store, 5, ["k1"])
    store.conn.execute("INSERT INTO play_events(identity_id, identity_key, played_at) VALUES (1,'k1',?)",
                       (2 * 86400.0,))    # play precedes the snapshot -> play_event wins the MIN
    store.conn.commit()
    _build(store)
    row = store.conn.execute(
        "SELECT first_day, source FROM trend_first_play WHERE kind='track' AND id_key='k1'").fetchone()
    assert row["first_day"] == 2 and row["source"] == "play_event"


def test_upsert_first_play_min_conflict_keeps_lower_ts(store):
    """Exercise the ON CONFLICT DO UPDATE CASE/MIN branch directly (not via _build), in both
    directions, for kind='track'."""
    # Seed k1 at day 5 (first_ts = 5*86400).
    store.trends.upsert_first_play_min([("track", "k1", 5, 5 * 86400.0, "history")])
    assert store.trends.first_play_map("track") == {"k1": 5}

    # Incoming ts = 2*86400 < 5*86400 -> LOWER, so MIN(5*86400, 2*86400) = 2*86400 -> day 2. Must win.
    store.trends.upsert_first_play_min([("track", "k1", 2, 2 * 86400.0, "play_event")])
    assert store.trends.first_play_map("track") == {"k1": 2}
    row = store.conn.execute(
        "SELECT source FROM trend_first_play WHERE kind='track' AND id_key='k1'").fetchone()
    assert row["source"] == "play_event"   # source follows the winning (lower-ts) row

    # Incoming ts = 7*86400 > 2*86400 -> HIGHER, so MIN(2*86400, 7*86400) stays 2*86400 -> day 2.
    # Must NOT overwrite the existing (lower) value or its source.
    store.trends.upsert_first_play_min([("track", "k1", 7, 7 * 86400.0, "history")])
    assert store.trends.first_play_map("track") == {"k1": 2}
    row = store.conn.execute(
        "SELECT source FROM trend_first_play WHERE kind='track' AND id_key='k1'").fetchone()
    assert row["source"] == "play_event"


def test_upsert_first_play_min_conflict_keeps_lower_ts_artist_kind(store):
    """Same MIN-preserving conflict path, but for kind='artist' rows. There is no artist-specific
    upsert primitive (rebuild_artist_first_play does a DELETE + full recompute from the track rows,
    not a conflict-preserving upsert) - upsert_first_play_min is generic over `kind`, so it is the
    only primitive that exercises this SQL for artist rows, called directly here."""
    # Seed A1 at day 5 (first_ts = 5*86400).
    store.trends.upsert_first_play_min([("artist", "A1", 5, 5 * 86400.0, "history")])
    assert store.trends.first_play_map("artist") == {"A1": 5}

    # LOWER incoming ts (day 2) must win.
    store.trends.upsert_first_play_min([("artist", "A1", 2, 2 * 86400.0, "play_event")])
    assert store.trends.first_play_map("artist") == {"A1": 2}

    # HIGHER incoming ts (day 7) must NOT overwrite.
    store.trends.upsert_first_play_min([("artist", "A1", 7, 7 * 86400.0, "history")])
    assert store.trends.first_play_map("artist") == {"A1": 2}


def test_max_snapshot_id_empty_and_after_inserts(store):
    # Contract (see trends.py): 0 when history_snapshots has no rows, not None.
    assert store.trends.max_snapshot_id() == 0

    sid1 = _snap(store, 2, ["k1"])
    assert store.trends.max_snapshot_id() == sid1

    sid2 = _snap(store, 5, ["k2"])
    assert sid2 > sid1
    assert store.trends.max_snapshot_id() == sid2


def test_clear_first_play_empties_table_and_rebuild_is_idempotent(store):
    for k, a in [("k1", "A1"), ("k2", "A2")]:
        _track(store, k, a)
    _snap(store, 3, ["k1"])
    _snap(store, 6, ["k2"])
    _build(store)

    tracks_before = store.trends.first_play_map("track")
    artists_before = store.trends.first_play_map("artist")
    assert tracks_before == {"k1": 3, "k2": 6}
    assert artists_before == {"A1": 3, "A2": 6}

    store.trends.clear_first_play()
    assert store.trends.first_play_map("track") == {}
    assert store.trends.first_play_map("artist") == {}
    assert store.conn.execute("SELECT COUNT(*) c FROM trend_first_play").fetchone()["c"] == 0

    # Rebuilding from the same underlying history/play_events must reproduce the same index exactly.
    _build(store)
    assert store.trends.first_play_map("track") == tracks_before
    assert store.trends.first_play_map("artist") == artists_before


def test_history_track_first_after_id_window(store):
    sid1 = _snap(store, 2, ["k1", "k2"])   # snapshot 1: day 2
    sid2 = _snap(store, 5, ["k3"])         # snapshot 2: day 5

    # Window strictly after sid1 -> only snapshot 2's rows are visible, even though sid1 still exists.
    windowed = store.trends.history_track_first(sid1)
    assert set(windowed) == {"k3"}
    assert windowed["k3"] == (5, 5 * 86400.0)

    # Window after sid2 -> nothing newer than the latest snapshot.
    assert store.trends.history_track_first(sid2) == {}

    # Window before both (after_id=0) -> sees everything.
    full = store.trends.history_track_first(0)
    assert set(full) == {"k1", "k2", "k3"}


def test_takeout_backfill_moves_first_play_earlier(store):
    """End-to-end: build the index on recent-only history, then simulate a Takeout backfill that
    inserts an OLDER snapshot. clear_first_play + full rebuild must move the first-play EARLIER to
    match the new, older minimum (an after_id-windowed incremental upsert would miss it, since the
    backfilled snapshot has a higher id but an older day than what's already indexed)."""
    _track(store, "k1", "A1")

    # Recent-only history: first evidence is day 10.
    _snap(store, 10, ["k1"])
    _build(store)
    recent = store.trends.first_play_map("track")
    assert recent == {"k1": 10}
    assert store.trends.first_play_map("artist") == {"A1": 10}

    # Takeout backfill arrives as a new snapshot (higher id) but for an OLDER day (day 1).
    _snap(store, 1, ["k1"])

    store.trends.clear_first_play()
    assert store.trends.first_play_map("track") == {}
    _build(store)

    backfilled = store.trends.first_play_map("track")
    # first play moved from day 10 to day 1.
    assert backfilled == {"k1": 1}
    assert backfilled["k1"] < recent["k1"]
    assert store.trends.first_play_map("artist") == {"A1": 1}
