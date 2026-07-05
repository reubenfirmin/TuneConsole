import pytest
from yt_playlist.core.store import Store
from yt_playlist.repos.base import GENERATED_GROUP


@pytest.fixture
def store():
    s = Store(":memory:"); s.init_schema()
    return s


def _ident(store):
    return store.upsert_identity("main", "ref", None, True)


def test_radio_playlist_play_is_quarantined_by_real_list_id(store):
    ident = _ident(store)
    # The app-managed radio playlist, tagged Generated (as create_generated_playlist does).
    pid = store.upsert_playlist(ident, "PLRADIO", "TuneConsole Radio", 0, "", 100000.0)
    store.set_playlist_group("PLRADIO", GENERATED_GROUP)
    # A play carrying the playlist's REAL ytm id as provenance. UTC day int(100000//86400) == 1.
    store.record_play_event(ident, "k1", "v1", 100000.0, playlist_ytm_id="PLRADIO")
    assert (1, "k1") in store.generated_only_play_days()
    assert "k1" in store.generated_only_keys_since(0.0)


def test_real_non_generated_play_not_quarantined(store):
    ident = _ident(store)
    store.record_play_event(ident, "k2", "v2", 100000.0, playlist_ytm_id=None)
    store.record_play_event(ident, "k3", "v3", 100000.0, playlist_ytm_id="PLreal")
    assert (1, "k2") not in store.generated_only_play_days()
    assert (1, "k3") not in store.generated_only_play_days()


# --- #93 plays_by_list_ids_since: the cross-session freshness cooldown's own query. Independent of
# the GENERATED_GROUP quarantine above -- it matches plays directly by an explicit set of ytm ids
# (the radio deck/playlist settings), with no playlist-group lookup involved. ---

def test_plays_by_list_ids_since_matches_given_ids_within_window(store):
    ident = _ident(store)
    store.record_play_event(ident, "kA", "vA", 100000.0, playlist_ytm_id="PLA")
    store.record_play_event(ident, "kB", "vB", 100000.0, playlist_ytm_id="PLB")
    store.record_play_event(ident, "kOther", "vOther", 100000.0, playlist_ytm_id="PLREAL")
    store.record_play_event(ident, "kNoList", "vNoList", 100000.0, playlist_ytm_id=None)
    assert store.plays_by_list_ids_since({"PLA", "PLB"}, 0.0) == {"kA", "kB"}


def test_plays_by_list_ids_since_excludes_plays_before_the_window(store):
    ident = _ident(store)
    store.record_play_event(ident, "kOld", "vOld", 100.0, playlist_ytm_id="PLA")
    store.record_play_event(ident, "kNew", "vNew", 1000.0, playlist_ytm_id="PLA")
    assert store.plays_by_list_ids_since({"PLA"}, 500.0) == {"kNew"}


def test_plays_by_list_ids_since_empty_ids_short_circuits_with_no_query(store):
    ident = _ident(store)
    store.record_play_event(ident, "k", "v", 100000.0, playlist_ytm_id="PLA")
    assert store.plays_by_list_ids_since(set(), 0.0) == set()
    assert store.plays_by_list_ids_since({None, ""}, 0.0) == set()   # falsy ids filtered, same as empty
