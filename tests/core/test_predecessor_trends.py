"""Trends remain usable while the installed listening owners await conversion."""
import json
import pytest

from tests.core.test_predecessor_writes import consumer_installed as installed
from tests.store_fixture_support import native_sql_calls, without_query_timing
from yt_playlist.rec import trend_rollups

pytestmark = pytest.mark.usefixtures('native_sql_calls', 'without_query_timing')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Rollups retain actual SQL and publication without HTTP startup."""


DAY = 86400
STAMP = 1704844800  # 2024-01-10 UTC


def seed(store):
    account = store.accounts.upsert_account('Main', 'main.json', None, True)
    entry = store.tracks.upsert_track('known', 'Song', 'Artist', None, 123)
    playlist = store.playlists.upsert_playlist(account, 'SOURCE', 'Saved', 1, '', STAMP)
    store.playlists.set_playlist_tracks(playlist, [entry])
    key = store.tracks.identity_key_for_video('known')
    store.history.record_history_plays(account, STAMP - 10 * DAY, [(key, 'today')], source_timezone='UTC')
    store.history.record_play_event(account, key, 'known', STAMP, 'SOURCE', source_timezone='UTC')
    return account, key


def test_full_rollup_retains_coarse_first_date_and_canonical_artist(installed):
    store, _ = installed
    account, key = seed(store)
    payload = trend_rollups.build(store, STAMP + 30 * DAY)
    assert payload is not None
    assert store.rec_proposals.get_proposals('trend_rollups') == payload
    assert store.trends.first_play_map('track')[key] == STAMP // DAY - 10
    artists = store.trends.first_play_map('artist')
    assert {store.trends.artist_labels()[artist]: day for artist, day in artists.items()} == {
        'Artist': STAMP // DAY - 10}
    assert store.trends.catalog_artists() == set(artists)
    # A late imported date must replace the first date; exact play counts stay exact.
    store.history.record_history_plays(account, STAMP - 20 * DAY, [(key, 'today')], source_timezone='UTC')
    assert trend_rollups.build(store, STAMP + 30 * DAY) is not None
    assert store.trends.first_play_map('track')[key] == STAMP // DAY - 20
    assert trend_rollups.day_counts_source(store) == [(STAMP // DAY, key, 1)]


def test_private_index_does_not_publish_over_continuing_evidence(installed):
    store, _ = installed
    account, key = seed(store)
    store.trends.refresh_first_play()
    before = list(map(tuple, store.conn.execute('SELECT * FROM trend_first_play')))
    with store.read_snapshot() as snapshot:
        prepared = snapshot.store.trends.prepare_first_play()
        assert snapshot.store.trends.first_play_map('track')[key] == STAMP // DAY - 10
        assert list(map(tuple, store.conn.execute('SELECT * FROM trend_first_play'))) == before
        store.history.record_history_plays(account, STAMP - 20 * DAY, [(key, 'today')], source_timezone='UTC')
        assert not snapshot.publish(lambda: store.trends.publish_rollup(prepared, json.dumps({}), STAMP, None))
    assert trend_rollups.build(store, STAMP + 30 * DAY) is not None
    assert store.trends.first_play_map('track')[key] == STAMP // DAY - 20
