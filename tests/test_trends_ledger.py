"""The play ledger (play_events) is the only source with real timestamps AND playlist provenance.
These tests pin the ledger read API the Trends rollup consumes.

The history day model it replaces counted a snapshot appearance as a play, so a track lingering in
YouTube's recently-played window was recorded as played again on every sync. On the reference database
that turned 2 real plays of one track into 14.
"""
import pytest

from yt_playlist.core.store import Store

DAY = 86400


@pytest.fixture
def store():
    s = Store(":memory:")
    s.init_schema()
    s.conn.execute("INSERT INTO identities(id, label, credential_ref, is_master) VALUES (1,'me','c.json',1)")
    s.conn.commit()
    return s


def _playlist(store, pid, ytm, title, group=None, ident=1):
    store.conn.execute(
        "INSERT INTO playlists(id, identity_id, ytm_playlist_id, title) VALUES (?,?,?,?)",
        (pid, ident, ytm, title))
    if group:
        store.conn.execute("INSERT INTO playlist_group(ytm, name) VALUES (?,?)", (ytm, group))
    store.conn.commit()


def _track(store, tid, key, artist, title="t"):
    store.conn.execute(
        "INSERT INTO tracks(id, identity_key, title, artist) VALUES (?,?,?,?)", (tid, key, title, artist))
    store.conn.commit()


def _event(store, key, day, playlist=None, ident=1, sec=0):
    store.conn.execute(
        "INSERT INTO play_events(identity_id, identity_key, video_id, played_at, playlist_ytm_id) "
        "VALUES (?,?,?,?,?)", (ident, key, "v" + key, day * DAY + sec, playlist))
    store.conn.commit()


# --- Task 1.1: generated_ytm_ids ------------------------------------------------------------------

def test_generated_ytm_ids_returns_only_the_generated_group(store):
    _playlist(store, 1, "PL_gen", "Radio: Techno", group="Generated")
    _playlist(store, 2, "PL_own", "My mixtape")
    _playlist(store, 3, "PL_alb", "Some album", group="Albums")
    assert store.trends.generated_ytm_ids() == {"PL_gen"}


def test_generated_ytm_ids_empty_when_no_generated_playlists(store):
    _playlist(store, 1, "PL_own", "My mixtape")
    assert store.trends.generated_ytm_ids() == set()


# --- Task 1.2: has_play_ledger --------------------------------------------------------------------

def test_has_play_ledger_false_when_empty(store):
    assert store.trends.has_play_ledger() is False


def test_has_play_ledger_true_once_an_event_exists(store):
    _event(store, "k", 1)
    assert store.trends.has_play_ledger() is True


# --- Task 1.3: ledger_day_counts ------------------------------------------------------------------

def test_ledger_day_counts_counts_real_plays_per_day(store):
    _event(store, "a", 10, sec=1)
    _event(store, "a", 10, sec=2)     # same track, same day, twice -> 2 plays
    _event(store, "b", 11, sec=1)
    assert sorted(store.trends.ledger_day_counts()) == [(10, "a", 2), (11, "b", 1)]


def test_ledger_day_counts_excludes_generated_playlist_plays(store):
    _playlist(store, 1, "PL_gen", "Radio: Techno", group="Generated")
    _event(store, "a", 10, playlist="PL_gen")
    _event(store, "a", 10, playlist=None, sec=5)   # organic play of the same track, same day
    _event(store, "b", 10, playlist="PL_gen")      # only ever played from a generated list
    assert sorted(store.trends.ledger_day_counts()) == [(10, "a", 1)]


def test_ledger_day_counts_can_keep_generated_when_asked(store):
    _playlist(store, 1, "PL_gen", "Radio", group="Generated")
    _event(store, "a", 10, playlist="PL_gen")
    assert store.trends.ledger_day_counts(exclude_generated=False) == [(10, "a", 1)]


def test_ledger_day_counts_keeps_plays_with_no_provenance(store):
    """Takeout backfill carries no playlist_ytm_id. A NULL cannot be proven generated, so it counts."""
    _playlist(store, 1, "PL_gen", "Radio", group="Generated")
    _event(store, "a", 10, playlist=None)
    assert store.trends.ledger_day_counts() == [(10, "a", 1)]


def test_ledger_day_counts_merges_identities_on_the_same_day(store):
    """The history model's (identity, day) dedupe double-counted a track played on two accounts."""
    store.conn.execute("INSERT INTO identities(id,label,credential_ref,is_master) VALUES (2,'brand','c.json',0)")
    store.conn.commit()
    _event(store, "a", 10, ident=1, sec=1)
    _event(store, "a", 10, ident=2, sec=2)
    assert store.trends.ledger_day_counts() == [(10, "a", 2)]   # two real plays, one row


def test_ledger_day_counts_empty_ledger(store):
    assert store.trends.ledger_day_counts() == []


# --- Task 1.4: ledger_track_plays / ledger_artist_plays -------------------------------------------

def test_ledger_track_plays_counts_real_plays_in_window(store):
    _event(store, "a", 10, sec=1)
    _event(store, "a", 10, sec=2)
    _event(store, "a", 11, sec=1)
    _event(store, "a", 99, sec=1)                      # outside the window
    assert store.trends.ledger_track_plays(10 * DAY, 12 * DAY) == {"a": 3}


def test_ledger_track_plays_is_not_inflated_by_lingering(store):
    """The bug this replaces: 2 real plays rendered as 14 because the track sat in the sync window."""
    _event(store, "a", 10)
    _event(store, "a", 19)
    assert store.trends.ledger_track_plays(10 * DAY, 30 * DAY) == {"a": 2}


def test_ledger_track_plays_excludes_generated(store):
    _playlist(store, 1, "PL_gen", "Radio", group="Generated")
    _event(store, "a", 10, playlist="PL_gen")
    _event(store, "a", 10, playlist=None, sec=5)
    assert store.trends.ledger_track_plays(10 * DAY, 11 * DAY) == {"a": 1}


def test_ledger_artist_plays_aggregates_by_artist(store):
    _track(store, 1, "a", "Underworld")
    _track(store, 2, "b", "Underworld")
    _track(store, 3, "c", "Orbital")
    _event(store, "a", 10)
    _event(store, "b", 10)
    _event(store, "c", 10)
    assert store.trends.ledger_artist_plays(10 * DAY, 11 * DAY) == {"Underworld": 2, "Orbital": 1}


def test_ledger_artist_plays_ignores_tracks_with_no_artist(store):
    _track(store, 1, "a", "")
    _event(store, "a", 10)
    assert store.trends.ledger_artist_plays(10 * DAY, 11 * DAY) == {}


def test_ledger_artist_plays_excludes_generated(store):
    _track(store, 1, "a", "Underworld")
    _playlist(store, 1, "PL_gen", "Radio", group="Generated")
    _event(store, "a", 10, playlist="PL_gen")
    _event(store, "a", 10, playlist=None, sec=5)
    assert store.trends.ledger_artist_plays(10 * DAY, 11 * DAY) == {"Underworld": 1}


# --- catalog membership: "new to you" vs "new to our logs" ----------------------------------------
# The play ledger begins when the extension or a Takeout export begins. An artist the user has known
# for a decade looks brand-new the first time we happen to observe a play. A track already sitting in
# one of their own playlists is prior evidence of familiarity: they moved a whole catalog over from
# another service and had listened to most of it.

def _in_playlist(store, pid, tid):
    store.conn.execute("INSERT INTO playlist_tracks(playlist_id, track_id, position) VALUES (?,?,0)",
                       (pid, tid))
    store.conn.commit()


def test_catalog_artists_counts_artists_in_owned_playlists(store):
    _playlist(store, 1, "PL_own", "My mixtape")
    _track(store, 1, "a", "Underworld")
    _in_playlist(store, 1, 1)
    assert store.trends.catalog_artists() == {"Underworld"}


def test_catalog_artists_ignores_generated_playlists(store):
    """The app putting an artist in front of you is not you knowing them."""
    _playlist(store, 1, "PL_gen", "Radio", group="Generated")
    _track(store, 1, "a", "Underworld")
    _in_playlist(store, 1, 1)
    assert store.trends.catalog_artists() == set()


def test_catalog_artists_counts_a_promoted_generated_playlist(store):
    """Graduation (repos/base.py): promote a generated playlist out of the group and it is yours."""
    _playlist(store, 1, "PL_prom", "Was generated, now mine")   # no Generated group row
    _track(store, 1, "a", "Underworld")
    _in_playlist(store, 1, 1)
    assert store.trends.catalog_artists() == {"Underworld"}


def test_catalog_artists_skips_blank_artists(store):
    _playlist(store, 1, "PL_own", "Mine")
    _track(store, 1, "a", "")
    _in_playlist(store, 1, 1)
    assert store.trends.catalog_artists() == set()


def test_catalog_track_keys_mirrors_catalog_artists(store):
    _playlist(store, 1, "PL_own", "Mine")
    _playlist(store, 2, "PL_gen", "Radio", group="Generated")
    _track(store, 1, "owned", "A")
    _track(store, 2, "suggested", "B")
    _in_playlist(store, 1, 1)
    _in_playlist(store, 2, 2)
    assert store.trends.catalog_track_keys() == {"owned"}
