"""#83 the engine must not learn co-occurrence from its own generated-playlist plays.

generated_only_play_days() returns (calendar_day, identity_key) pairs where EVERY play_events row for
that key on that account calendar day carries generated-playlist provenance (played from a quarantined generated
playlist, or its radio), so rec_baskets can drop that day's session basket for that key instead of
re-learning co-occurrence from a suggestion the app made to itself. Any NULL-playlist or ordinary-
playlist play that day is real evidence, so it keeps the (day, key) OUT of the set (conservative).
"""
from yt_playlist.repos.base import GENERATED_GROUP

_DAY = 86400


def _mark_generated(store, ytm):
    store.playlists.set_playlist_group(ytm, GENERATED_GROUP)
    store.playlists.mark_generated_playlist(ytm)


def test_generated_only_play_is_in_the_set(store):
    iid = store.identities.upsert_identity("me", "c", None, True)
    store.playlists.upsert_playlist(iid, "PLGEN", "Gen mix", 0, "h", 1.0)
    _mark_generated(store, "PLGEN")
    store.history.record_play_event(iid, "a|x", "v1", 10.0, playlist_ytm_id="PLGEN", source_timezone='UTC')
    assert (0, "a|x") in store.rec_evidence.generated_only_play_days()


def test_mixed_evidence_is_excluded(store):
    # Spaced beyond the live-dedup merge window (30 min) so these land as two distinct rows.
    iid = store.identities.upsert_identity("me", "c", None, True)
    store.playlists.upsert_playlist(iid, "PLGEN", "Gen mix", 0, "h", 1.0)
    _mark_generated(store, "PLGEN")
    store.history.record_play_event(iid, "a|x", "v1", 10.0, playlist_ytm_id="PLGEN", source_timezone='UTC')
    store.history.record_play_event(iid, "a|x", "v2", 3000.0, playlist_ytm_id=None, source_timezone='UTC')
    assert (0, "a|x") not in store.rec_evidence.generated_only_play_days()


def test_radio_of_generated_playlist_counts_as_generated(store):
    iid = store.identities.upsert_identity("me", "c", None, True)
    store.playlists.upsert_playlist(iid, "PLGEN", "Gen mix", 0, "h", 1.0)
    _mark_generated(store, "PLGEN")
    store.history.record_play_event(iid, "a|x", "v1", 10.0, playlist_ytm_id="RDAMPLPLGEN", source_timezone='UTC')
    assert (0, "a|x") in store.rec_evidence.generated_only_play_days()


def test_ordinary_playlist_play_is_excluded(store):
    iid = store.identities.upsert_identity("me", "c", None, True)
    store.playlists.upsert_playlist(iid, "PLGEN", "Gen mix", 0, "h", 1.0)
    _mark_generated(store, "PLGEN")
    store.playlists.upsert_playlist(iid, "PLORD", "My playlist", 0, "h", 1.0)
    store.history.record_play_event(iid, "a|x", "v1", 10.0, playlist_ytm_id="PLORD", source_timezone='UTC')
    assert (0, "a|x") not in store.rec_evidence.generated_only_play_days()


def test_day_partitioning_keeps_only_the_generated_only_day(store):
    iid = store.identities.upsert_identity("me", "c", None, True)
    store.playlists.upsert_playlist(iid, "PLGEN", "Gen mix", 0, "h", 1.0)
    _mark_generated(store, "PLGEN")
    # Day 0: generated-only.
    store.history.record_play_event(iid, "a|x", "v1", 10.0, playlist_ytm_id="PLGEN", source_timezone='UTC')
    # Day 1: organic (ordinary playlist).
    store.playlists.upsert_playlist(iid, "PLORD", "My playlist", 0, "h", 1.0)
    store.history.record_play_event(iid, "a|x", "v2", _DAY + 10.0, playlist_ytm_id="PLORD", source_timezone='UTC')
    days = store.rec_evidence.generated_only_play_days()
    assert (0, "a|x") in days
    assert (1, "a|x") not in days
