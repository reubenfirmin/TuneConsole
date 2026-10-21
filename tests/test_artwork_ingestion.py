"""Artwork precedence survives ingestion from overlapping library and discovery sources."""
import pytest

from tests.conftest import FakeClient, _track
from tests.test_cover_art import _DEEZER, _REAL, _YT_ALBUM_PH, _YT_TRACK_PH
from yt_playlist.core import catalog
from yt_playlist.library.sync import refresh_playlist, sync_identity


def _art(store, entry):
    return store.tracks.catalog_subject(entry)["thumbnail"]


@pytest.mark.parametrize("incoming", [_YT_TRACK_PH, _YT_ALBUM_PH, None, "", "   "])
@pytest.mark.parametrize("existing", [_REAL, _DEEZER])
def test_ingestion_preserves_useful_art_when_new_art_is_unavailable(store, incoming, existing):
    entry = store.tracks.upsert_track("video", "Song", "Artist", "Album", 200, thumbnail=existing)
    for _ in range(2):
        assert store.tracks.upsert_track("video", "Song", "Artist", "Album", 200,
                                  thumbnail=incoming) == entry
        assert _art(store, entry) == existing


@pytest.mark.parametrize("placeholder", [_YT_TRACK_PH, _YT_ALBUM_PH])
def test_provider_recovery_survives_repeated_full_and_single_playlist_syncs(store, placeholder):
    iid = store.identities.upsert_identity("Main", "bridge", None, True)
    client = FakeClient(playlists=[{"playlistId": "PL", "title": "Library"}], tracks={"PL": [
        {**_track("video", "Song", "Artist"), "thumbnails": [{"url": placeholder}]}]})
    sync_identity(store, iid, client, now=100)
    entry = store.tracks.track_ids_for_videos(["video"])["video"]
    store.tracks.set_track_art(entry, _DEEZER)
    for now in (200, 300):
        sync_identity(store, iid, client, now=now)
        refresh_playlist(store, iid, client, "PL", "Library", now)
        assert _art(store, entry) == _DEEZER
    client._tracks["PL"][0]["thumbnails"] = [{"url": _REAL}]
    refresh_playlist(store, iid, client, "PL", "Library", 400)
    assert _art(store, entry) == _REAL
    newer = _REAL + "-new"
    client._tracks["PL"][0]["thumbnails"] = [{"url": newer}]
    sync_identity(store, iid, client, now=500)
    assert _art(store, entry) == newer
    store.tracks.set_track_art(entry, _DEEZER)
    assert _art(store, entry) == newer


@pytest.mark.parametrize("first_source", ["library", "discovery"])
def test_discovery_and_library_share_artwork_precedence(store, first_source):
    def library(art):
        return store.tracks.upsert_track("video", "Song", "Artist", "Album", 200, thumbnail=art)

    def discovery(art):
        store.discovery.upsert_discovered_track("song|artist", "video", "Song", "Artist", "Album", art,
                                      None, None, "BROWSE", 100)
        return store.tracks.track_ids_for_videos(["video"])["video"]

    first, second = (library, discovery) if first_source == "library" else (discovery, library)
    entry = first(_YT_TRACK_PH)
    store.tracks.set_track_art(entry, _DEEZER)
    assert second(_YT_ALBUM_PH) == entry
    assert _art(store, entry) == _DEEZER
    second(_REAL)
    assert _art(store, entry) == _REAL


@pytest.mark.parametrize("choice", ["https://chosen.example/cover.jpg", ""])
def test_explicit_artwork_choice_survives_ingestion_and_provider_fallback(store, choice):
    entry = store.tracks.upsert_track("video", "Song", "Artist", "Album", 200, thumbnail=_YT_TRACK_PH)
    with store.conn:
        catalog.choose_field(store.conn, entry, "thumbnail", choice)
    store.tracks.upsert_track("video", "Song", "Artist", "Album", 200, thumbnail=_REAL)
    store.tracks.set_track_art(entry, _DEEZER)
    assert _art(store, entry) == choice
    assert store.conn.execute("SELECT thumbnail FROM youtube_items WHERE video_id='video'").fetchone()[0] == _YT_TRACK_PH


@pytest.mark.parametrize("incoming", [_YT_TRACK_PH, _YT_ALBUM_PH])
def test_provider_placeholder_is_never_accepted_as_recovery(store, incoming):
    entry = store.tracks.upsert_track(None, "Song", "Artist", "Album", 200, source_ref="unresolved")
    store.tracks.set_track_art(entry, incoming)
    assert _art(store, entry) is None
    store.tracks.set_track_art(entry, _DEEZER)
    store.tracks.upsert_track(None, "Song", "Artist", "Album", 200, thumbnail=incoming, source_ref="unresolved")
    assert _art(store, entry) == _DEEZER
