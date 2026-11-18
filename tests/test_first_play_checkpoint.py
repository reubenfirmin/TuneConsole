"""Account changes cannot strand history behind an already advanced first-play checkpoint."""
from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from tests.genre_catalog_support import bound_catalog
from tests.historical_captures import retained_capture
from tests.recommendation_support import seed_publication_subjects
from tests.store_fixture_support import bounded_consumer_schema, initialize_consumer
from yt_playlist.core.migrations import migrate
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.store import Store

DAY = 86400


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These SQL/index checks do not construct the web application."""


def _history(store, key, day):
    return store.history.record_history_plays(1, day * DAY, [key], source_timezone="UTC")


def _assert_first(store, key, artist, day):
    store.trends.refresh_first_play()
    assert store.trends.first_play_map("track")[key] == day
    by_name = {store.trends.artist_labels()[aid]:first for aid,first in store.trends.first_play_map('artist').items()}
    assert by_name[artist] == day
    assert store.settings.get_setting('trend_first_play_watermark') is None
    assert store.settings.get_setting('trend_first_play_source') == 'canonical_listening'


def test_alias_disambiguation_rebuilds_renamed_history(store):
    store.accounts.upsert_account("main", "bridge", None, True)
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
    store.accounts.upsert_account("main", "bridge", None, True)
    store.tracks.upsert_track("a", "Song", "Artist A", None, 200)
    target = store.tracks.upsert_track("b", "Correct Song", "Artist B", None, 200)
    old, key = store.tracks.identity_key_for_video("a"), store.tracks.identity_key_for_video("b")
    _history(store, old, 10); _history(store, key, 20)
    store.trends.refresh_first_play()
    store.tracks.associate_youtube_item("a", store.tracks.catalog_subject(target)["recording_id"],
        source="verified", evidence={"review": "same recording"}, now=100)
    _assert_first(store, key, "Artist B", 10)
    assert old not in store.trends.first_play_map("track")
    assert store.conn.execute("SELECT COUNT(*) FROM listening_observations").fetchone()[0] == 2


def test_interruption_rolls_back_index_and_checkpoint(store, monkeypatch):
    store.accounts.upsert_account("main", "bridge", None, True)
    store.tracks.upsert_track("a", "Song", "Artist", None, 200)
    key = store.tracks.identity_key_for_video("a")
    _history(store, key, 20); _assert_first(store, key, "Artist", 20)
    _history(store, key, 10)
    # Current ingestion invalidates the published index before a refresh starts.
    assert store.trends.first_play_map("track") == {}
    watermark = store.settings.get_setting("trend_first_play_watermark")
    before = list(map(tuple, store.conn.execute("SELECT * FROM trend_first_play")))
    original = store.trends.repository._replace_first_play
    def interrupted():
        original()
        raise RuntimeError("interrupted after index writes")
    monkeypatch.setattr(store.trends.repository, "_replace_first_play", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        store.trends.refresh_first_play()
    assert list(map(tuple, store.conn.execute("SELECT * FROM trend_first_play"))) == before
    assert store.settings.get_setting("trend_first_play_watermark") == watermark
    monkeypatch.setattr(store.trends.repository, "_replace_first_play", original)
    _assert_first(store, key, "Artist", 10)


def test_exact_ledger_preserves_earlier_coarse_evidence(store):
    store.accounts.upsert_account("main", "bridge", None, True)
    store.tracks.upsert_track("a", "Song", "Artist", None, 200)
    key = store.tracks.identity_key_for_video("a")
    _history(store, key, 10); _assert_first(store, key, "Artist", 10)
    store.history.record_play_event(1, key, "a", 20 * DAY)
    _assert_first(store, key, "Artist", 10)
