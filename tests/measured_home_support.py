"""Measured Home input retained for prepared-fixture isolation checks."""
from tests.home_fixture_support import returning_listener
from tests.test_mode_cards_route import prepare_home
from yt_playlist.util.matching import identity_key


def prepare(store):
    returning_listener(store, 'vibes', now=1)
    store.settings.set_setting("last_sync_at", "1")
    store.settings.set_setting("onboard_dismissed", "1")
    iid = store.accounts.get_accounts()[0].id
    for i, genre in enumerate(["Jazz", "Techno", "Trance", "Indie Rock", "Ambient", "Classical"]):
        tid = store.tracks.upsert_track(f"favorite-{i}", f"Favorite {i}", f"Favorite artist {i}", None, None)
        store.tracks.set_track_genre(tid, genre)
        store.tracks.set_track_year(tid, 1960 + i * 10)
        for day in range(10):
            store.history.record_history_plays(iid, 1 - day * 86400,
                [identity_key(f"Favorite {i}", f"Favorite artist {i}")], source_timezone="UTC")
    prepare_home(store, 1)
    assert store.recommendations.candidate_page('fresh', 'all', 'fresh', limit=1)['tracks']
