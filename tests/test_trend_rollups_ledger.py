"""build() must read the play ledger when one exists and fall back to the coarse history day model
when it does not (fresh install: no extension, no Takeout import).

The history model counts snapshot appearances, so a track lingering in YouTube's recently-played window
is recorded as played again on every sync. On the reference database 79% of sync-era history rows have
no corresponding real play, and that is what put 131 tracks and 32 artists on a single "first seen" day
and drove the discovery rate to 42%.
"""
import datetime as dt

import pytest

from yt_playlist.core.store import Store
from yt_playlist.rec import trend_rollups as tr

DAY = 86400


@pytest.fixture
def store():
    s = Store(":memory:")
    s.init_schema()
    s.conn.execute("INSERT INTO identities(id,label,credential_ref,is_master) VALUES (1,'me','c.json',1)")
    s.conn.commit()
    return s


def _ev(store, key, when, playlist=None):
    ts = when.timestamp() if isinstance(when, dt.datetime) else float(when)
    store.conn.execute(
        "INSERT INTO play_events(identity_id,identity_key,video_id,played_at,playlist_ytm_id) "
        "VALUES (1,?,?,?,?)", (key, "v", ts, playlist))
    store.conn.commit()


def _generated(store, pid, ytm, title="Radio"):
    store.conn.execute("INSERT INTO playlists(id,identity_id,ytm_playlist_id,title) VALUES (?,1,?,?)",
                       (pid, ytm, title))
    store.conn.execute("INSERT INTO playlist_group(ytm,name) VALUES (?,'Generated')", (ytm,))
    store.conn.commit()


# --- Task 2.1: day_counts_source ------------------------------------------------------------------

def test_day_counts_source_uses_history_when_no_ledger(store):
    store.history.add_history_snapshot(1, 10 * DAY, ["a", "a", "b"])
    assert sorted(tr.day_counts_source(store)) == [(10, "a", 2), (10, "b", 1)]


def test_day_counts_source_prefers_the_ledger(store):
    store.history.add_history_snapshot(1, 10 * DAY, ["phantom"])
    _ev(store, "real", 10 * DAY + 5)
    assert tr.day_counts_source(store) == [(10, "real", 1)]   # the phantom history row is ignored


def test_day_counts_source_applies_the_generated_quarantine(store):
    _generated(store, 1, "PL_gen")
    _ev(store, "a", 10 * DAY, playlist="PL_gen")
    _ev(store, "b", 10 * DAY + 5)
    assert tr.day_counts_source(store) == [(10, "b", 1)]


# --- Task 2.2: the first-play index ---------------------------------------------------------------

def test_first_play_index_ignores_history_when_a_ledger_exists(store):
    """A phantom history row must not drag a track's first-seen day earlier and fake a discovery."""
    store.history.add_history_snapshot(1, 10 * DAY, ["a"])
    _ev(store, "a", 20 * DAY)
    tr._build_first_play_index(store)
    assert store.trends.first_play_map("track")["a"] == 20


def test_first_play_index_uses_history_without_a_ledger(store):
    store.history.add_history_snapshot(1, 10 * DAY, ["a"])
    tr._build_first_play_index(store)
    assert store.trends.first_play_map("track")["a"] == 10


# --- Task 2.3: month_review -----------------------------------------------------------------------

def _months(store):
    return tr.compute_months(tr.day_counts_source(store), store.trends.track_meta(),
                             store.trends.first_play_map("artist"))


def test_month_review_song_of_the_month_counts_real_plays(store):
    store.conn.execute("INSERT INTO tracks(id,identity_key,title,artist) VALUES (1,'a','Hey You','Ben')")
    store.conn.commit()
    for d in (3, 12):
        _ev(store, "a", dt.datetime(2026, 6, d, 12, tzinfo=dt.UTC))
    now = dt.datetime(2026, 7, 9, tzinfo=dt.UTC).timestamp()
    review = tr.month_review(_months(store), store, now, tr.day_counts_source(store),
                             store.trends.track_meta())
    assert review["month"] == "2026-06"
    assert review["top_track"]["plays"] == 2      # not 14


def test_month_review_top_artists_exclude_generated_playlist_plays(store):
    store.conn.execute("INSERT INTO tracks(id,identity_key,title,artist) VALUES (1,'a','T','Underworld')")
    store.conn.execute("INSERT INTO tracks(id,identity_key,title,artist) VALUES (2,'b','T2','Orbital')")
    store.conn.commit()
    _generated(store, 1, "PL_gen")
    _ev(store, "a", dt.datetime(2026, 6, 3, 12, tzinfo=dt.UTC), playlist="PL_gen")
    _ev(store, "b", dt.datetime(2026, 6, 4, 12, tzinfo=dt.UTC))
    now = dt.datetime(2026, 7, 9, tzinfo=dt.UTC).timestamp()
    review = tr.month_review(_months(store), store, now, tr.day_counts_source(store),
                             store.trends.track_meta())
    artists = [a["artist"] for a in review["top_artists"]]
    assert artists == ["Orbital"]                # Underworld was only ever played from a generated list


def test_month_review_falls_back_to_history_without_a_ledger(store):
    store.conn.execute("INSERT INTO tracks(id,identity_key,title,artist) VALUES (1,'a','T','Ben')")
    store.conn.commit()
    store.history.add_history_snapshot(1, int(dt.datetime(2026, 6, 3, 12, tzinfo=dt.UTC).timestamp()), ["a"])
    now = dt.datetime(2026, 7, 9, tzinfo=dt.UTC).timestamp()
    review = tr.month_review(_months(store), store, now, tr.day_counts_source(store),
                             store.trends.track_meta())
    assert review["month"] == "2026-06"
    assert review["top_track"]["plays"] == 1


# --- discovery is catalog-relative, not log-relative ----------------------------------------------
# The user migrated a whole library from another service and had already listened to most of it. The
# play ledger only starts when the extension or a Takeout export starts, so first-observed-play alone
# reports them "discovering" their own collection.

def _owned(store, pid, tid, key, artist, ytm="PL_own", generated=False):
    store.conn.execute("INSERT OR IGNORE INTO playlists(id,identity_id,ytm_playlist_id,title) "
                       "VALUES (?,1,?,'p')", (pid, ytm))
    if generated:
        store.conn.execute("INSERT OR IGNORE INTO playlist_group(ytm,name) VALUES (?,'Generated')", (ytm,))
    store.conn.execute("INSERT INTO tracks(id,identity_key,title,artist) VALUES (?,?,'t',?)",
                       (tid, key, artist))
    store.conn.execute("INSERT INTO playlist_tracks(playlist_id,track_id,position) VALUES (?,?,0)",
                       (pid, tid))
    store.conn.commit()


def test_compute_weeks_does_not_call_a_catalog_artist_new():
    """An artist already in your playlists is not a discovery, whenever we first happened to see them."""
    dc = [(10, "a", 3)]
    meta = {"a": ("Underworld", None)}
    weeks = tr.compute_weeks(dc, meta, {"Underworld": 10}, {"a": 10},
                             catalog_artists={"Underworld"}, catalog_keys={"a"})
    assert weeks[0]["new_artist_plays"] == 0
    assert weeks[0]["new_artists"] == 0
    assert weeks[0]["new_track_plays"] == 0


def test_compute_weeks_counts_an_artist_absent_from_the_catalog():
    dc = [(10, "a", 3)]
    meta = {"a": ("Brand New Act", None)}
    weeks = tr.compute_weeks(dc, meta, {"Brand New Act": 10}, {"a": 10}, catalog_artists=set())
    assert weeks[0]["new_artist_plays"] == 3
    assert weeks[0]["new_artists"] == 1


def test_compute_months_respects_the_catalog(store):
    dc = [(10, "a", 2), (10, "b", 1)]
    meta = {"a": ("Owned", None), "b": ("Fresh", None)}
    af = {"Owned": 10, "Fresh": 10}
    months = tr.compute_months(dc, meta, af, catalog_artists={"Owned"})
    assert months[0]["new_artist_plays"] == 1 and months[0]["new_artists"] == 1


def test_build_treats_a_migrated_library_as_known(store):
    """End to end: a track in an owned playlist, played for the first time today, is not a discovery."""
    _owned(store, 1, 1, "a", "Underworld")
    _ev(store, "a", 10 * DAY)
    _ev(store, "a", 11 * DAY)
    tr._build_first_play_index(store)
    dc = tr.day_counts_source(store)
    weeks = tr.compute_weeks(dc, store.trends.track_meta(),
                             store.trends.first_play_map("artist"),
                             store.trends.first_play_map("track"),
                             store.trends.catalog_artists(), store.trends.catalog_track_keys())
    assert sum(w["new_artist_plays"] for w in weeks) == 0


def test_build_still_finds_a_true_discovery(store):
    """Played, but in no playlist of yours: that is brand new."""
    _owned(store, 1, 1, "owned", "Underworld")
    store.conn.execute("INSERT INTO tracks(id,identity_key,title,artist) VALUES (2,'fresh','t','Newcomer')")
    store.conn.commit()
    _ev(store, "fresh", 10 * DAY)
    tr._build_first_play_index(store)
    dc = tr.day_counts_source(store)
    weeks = tr.compute_weeks(dc, store.trends.track_meta(),
                             store.trends.first_play_map("artist"),
                             store.trends.first_play_map("track"),
                             store.trends.catalog_artists(), store.trends.catalog_track_keys())
    assert sum(w["new_artist_plays"] for w in weeks) == 1
    assert sum(w["new_artists"] for w in weeks) == 1


def test_a_generated_playlist_does_not_make_an_artist_familiar(store):
    """The app putting an artist in front of you is not you knowing them (repos/base.py quarantine)."""
    _owned(store, 1, 1, "sug", "Suggested Act", ytm="PL_gen", generated=True)
    _ev(store, "sug", 10 * DAY)                     # played, but NOT from the generated playlist
    tr._build_first_play_index(store)
    dc = tr.day_counts_source(store)
    weeks = tr.compute_weeks(dc, store.trends.track_meta(),
                             store.trends.first_play_map("artist"),
                             store.trends.first_play_map("track"),
                             store.trends.catalog_artists(), store.trends.catalog_track_keys())
    assert sum(w["new_artists"] for w in weeks) == 1
