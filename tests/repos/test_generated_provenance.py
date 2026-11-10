"""Causal evaluation excludes only generated evidence within its explicit window.

Organic or independent evidence in that same interval keeps a key eligible.
Daily windows and a window spanning several days need not exclude the same keys.
"""
from yt_playlist.repos.base import GENERATED_GROUP

_DAY = 86400


def _mark_generated(store, ytm):
    store.playlists.set_playlist_group(ytm, GENERATED_GROUP)
    store.playlists.mark_generated_playlist(ytm)


def test_generated_only_play_is_in_the_set(store):
    iid = store.accounts.upsert_account("me", "c", None, True)
    store.playlists.upsert_playlist(iid, "PLGEN", "Gen mix", 0, "h", 1.0)
    _mark_generated(store, "PLGEN")
    store.history.record_play_event(iid, "a|x", "v1", 10.0, playlist_ytm_id="PLGEN", source_timezone='UTC')
    assert store.rec_evidence.generated_only_keys_between(0, _DAY) == {"a|x"}


def test_mixed_evidence_is_excluded(store):
    # Spaced beyond the live-dedup merge window (30 min) so these land as two distinct rows.
    iid = store.accounts.upsert_account("me", "c", None, True)
    store.playlists.upsert_playlist(iid, "PLGEN", "Gen mix", 0, "h", 1.0)
    _mark_generated(store, "PLGEN")
    store.history.record_play_event(iid, "a|x", "v1", 10.0, playlist_ytm_id="PLGEN", source_timezone='UTC')
    store.history.record_play_event(iid, "a|x", "v2", 3000.0, playlist_ytm_id=None, source_timezone='UTC')
    assert store.rec_evidence.generated_only_keys_between(0, _DAY) == set()


def test_radio_of_generated_playlist_counts_as_generated(store):
    iid = store.accounts.upsert_account("me", "c", None, True)
    store.playlists.upsert_playlist(iid, "PLGEN", "Gen mix", 0, "h", 1.0)
    _mark_generated(store, "PLGEN")
    store.history.record_play_event(iid, "a|x", "v1", 10.0, playlist_ytm_id="RDAMPLPLGEN", source_timezone='UTC')
    assert store.rec_evidence.generated_only_keys_between(0, _DAY) == {"a|x"}


def test_ordinary_playlist_play_is_excluded(store):
    iid = store.accounts.upsert_account("me", "c", None, True)
    store.playlists.upsert_playlist(iid, "PLGEN", "Gen mix", 0, "h", 1.0)
    _mark_generated(store, "PLGEN")
    store.playlists.upsert_playlist(iid, "PLORD", "My playlist", 0, "h", 1.0)
    store.history.record_play_event(iid, "a|x", "v1", 10.0, playlist_ytm_id="PLORD", source_timezone='UTC')
    assert store.rec_evidence.generated_only_keys_between(0, _DAY) == set()


def test_window_scope_and_inclusive_start_exclusive_end(store):
    iid = store.accounts.upsert_account("me", "c", None, True)
    store.playlists.upsert_playlist(iid, "PLGEN", "Gen mix", 0, "h", 1.0)
    _mark_generated(store, "PLGEN")
    # Day 0: generated-only.
    store.history.record_play_event(iid, "a|x", "v1", 10.0, playlist_ytm_id="PLGEN", source_timezone='UTC')
    # Day 1: organic (ordinary playlist).
    store.playlists.upsert_playlist(iid, "PLORD", "My playlist", 0, "h", 1.0)
    store.history.record_play_event(iid, "a|x", "v2", _DAY + 10.0, playlist_ytm_id="PLORD", source_timezone='UTC')
    assert store.rec_evidence.generated_only_keys_between(0, _DAY) == {"a|x"}
    assert store.rec_evidence.generated_only_keys_between(_DAY, 2 * _DAY) == set()
    # Independent evidence on day 1 makes the combined window mixed.
    assert store.rec_evidence.generated_only_keys_between(0, 2 * _DAY) == set()
    # Include the generated play at start; exclude the organic play at end.
    assert store.rec_evidence.generated_only_keys_between(10, _DAY + 10) == {"a|x"}
    assert store.rec_evidence.generated_only_keys_between(0, 10) == set()
