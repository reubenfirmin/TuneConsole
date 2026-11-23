"""Recording changes immediately update the derived first-play dates."""

import pytest

DAY = 86400


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These SQL/index checks do not construct the web application."""


def _history(store, key, day):
    return store.history.record_history_plays(1, day * DAY, [key], source_timezone="UTC")


def _assert_first(store, key, artist, day):
    assert store.trends.first_play_map("track")[key] == day
    by_name = {store.trends.artist_labels()[aid]:first for aid,first in store.trends.first_play_map('artist').items()}
    assert by_name[artist] == day
    assert store.settings.get_setting('trend_first_play_watermark') is None


def test_alias_disambiguation_rebuilds_renamed_history(store):
    store.accounts.upsert_account("main", None, True)
    first = store.tracks.upsert_track("a", "Song", "Artist", None, 200)
    old = store.tracks.identity_key_for_video("a")
    _history(store, old, 10); _history(store, old, 20)
    _assert_first(store, old, "Artist", 10)
    store.tracks.upsert_track("b", "Song", "Artist", None, 200)
    renamed = store.tracks.catalog_subject(first)["identity_key"]
    assert renamed != old
    assert store.settings.get_setting("trend_first_play_watermark") is None
    _assert_first(store, renamed, "Artist", 10)
    assert old not in store.trends.first_play_map("track")
    assert store.conn.execute("SELECT COUNT(*) FROM listening_observations").fetchone()[0] == 2


def test_verified_reassociation_preserves_earliest_history(store):
    store.accounts.upsert_account("main", None, True)
    store.tracks.upsert_track("a", "Song", "Artist A", None, 200)
    target = store.tracks.upsert_track("b", "Correct Song", "Artist B", None, 200)
    old, key = store.tracks.identity_key_for_video("a"), store.tracks.identity_key_for_video("b")
    _history(store, old, 10); _history(store, key, 20)
    store.tracks.associate_youtube_item("a", store.tracks.catalog_subject(target)["recording_id"],
        source="verified", evidence={"review": "same recording"}, now=100, same_recording=True)
    _assert_first(store, key, "Artist B", 10)
    assert old not in store.trends.first_play_map("track")
    assert store.conn.execute("SELECT COUNT(*) FROM listening_observations").fetchone()[0] == 2


def test_earlier_history_is_visible_without_a_checkpoint(store):
    store.accounts.upsert_account("main", None, True)
    store.tracks.upsert_track("a", "Song", "Artist", None, 200)
    key = store.tracks.identity_key_for_video("a")
    _history(store, key, 20); _assert_first(store, key, "Artist", 20)
    _history(store, key, 10)
    _assert_first(store, key, "Artist", 10)


def test_exact_ledger_preserves_earlier_coarse_evidence(store):
    store.accounts.upsert_account("main", None, True)
    store.tracks.upsert_track("a", "Song", "Artist", None, 200)
    key = store.tracks.identity_key_for_video("a")
    _history(store, key, 10); _assert_first(store, key, "Artist", 10)
    store.history.record_play_event(1, key, "a", 20 * DAY)
    _assert_first(store, key, "Artist", 10)
