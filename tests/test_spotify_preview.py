import json

from yt_playlist.web.spotify_import import TransferPreviews, save_job


def test_preview_expiry_capacity_and_single_use():
    cache = TransferPreviews(ttl=10, capacity=2)
    oldest = cache.put({"source": "oldest"}, 0)
    other = cache.put({"source": "other"}, 1)
    newest = cache.put({"source": "newest"}, 2)
    assert cache.get(oldest, 2) is None
    assert cache.get(other, 2, consume=True) == {"source": "other"}
    assert cache.get(other, 2) is None
    assert cache.get(newest, 12) is None


def test_saved_playlist_report_discards_source_song_metadata(store):
    save_job(store, {"report_schema": 5, "report": {"details": {
        "playlists": [{"name": "Source playlist", "ytm_playlist_id": "PL1"}],
        "unmatched_tracks": [{"title": "Private source song", "spotify_url": "spotify:track:secret"}],
        "albums": []}}})
    saved = store.settings.get_setting("spotify_library_import_job")
    assert "Private source song" not in saved and "spotify:track:secret" not in saved
    assert json.loads(saved)["report"]["details"]["playlists"][0]["ytm_playlist_id"] == "PL1"
