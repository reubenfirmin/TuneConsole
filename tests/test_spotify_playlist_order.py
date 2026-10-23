"""Recovery fills source positions without replacing or deleting destination additions."""
from collections import Counter
import json

import pytest

from tests.conftest import _track
from tests.test_spotify_library_import import SongClient, _playlists_zip
from yt_playlist.library.spotify_library import import_spotify_library


class RecoveringClient(SongClient):
    failed_title = "One"
    lost_response = None
    reject_move = False

    def search(self, query, filter="songs"):
        if query == f"{self.failed_title} Artist":
            raise TimeoutError("lookup unavailable")
        tracks = super().search(query, filter)
        for track in tracks:
            track["setVideoId"] = f"set-{track['videoId']}"
        return tracks

    def add_playlist_items(self, playlist, videos):
        result = super().add_playlist_items(playlist, videos)
        if self.lost_response == "add":
            self.lost_response = None
            raise TimeoutError("add response lost")
        return result

    def edit_playlist(self, playlist, **kwargs):
        if self.reject_move:
            return "STATUS_SUCCEEDED"  # An acknowledgement alone cannot establish order.
        result = super().edit_playlist(playlist, **kwargs)
        if self.lost_response == "move":
            self.lost_response = None
            raise TimeoutError("move response lost")
        return result


@pytest.fixture
def transfer(store, monkeypatch):
    monkeypatch.setattr("yt_playlist.library.spotify_library.with_retry", lambda fn: fn())
    identity = store.identities.upsert_identity("Main", "bridge", None, True)
    raw = _playlists_zip(("Source", ["One", "Two", "Three"]))
    client = RecoveringClient()
    return store, identity, raw, client


def _run(t, now=100):
    store, identity, raw, client = t
    return import_spotify_library(store, raw, client, identity, now)


def _order(t):
    return [track["videoId"] for track in t[3]._tracks[t[3].created[0][0]]]


def _state(t):
    store, identity, _, _ = t
    return next(iter(json.loads(store.settings.get_setting(f"spotify_library_imported_playlists:{identity}")).values()))


@pytest.mark.parametrize("failed_title", ["One", "Two"])
def test_first_or_middle_lookup_recovers_source_order_idempotently(transfer, failed_title):
    t = transfer
    t[3].failed_title = failed_title
    first = _run(t)
    assert first["playlists_failed"] == 1
    assert not _state(t)["complete"]
    additions = len(t[3].added)
    _run(t, 101)  # Still disconnected: retry must not append the existing matches again.
    assert len(t[3].added) == additions
    t[3].failed_title = None
    assert _run(t, 102)["playlists_imported"] == 1
    assert _order(t) == ["yt-One", "yt-Two", "yt-Three"]
    assert _state(t)["source_video_ids"] == _order(t)
    assert _state(t)["complete"] and _state(t)["order_verified"]
    additions = len(t[3].added)
    assert _run(t, 103)["playlists_skipped"] == 1
    assert len(t[3].created) == 1 and len(t[3].added) == additions
    assert not t[3].removed and not t[3].deleted


@pytest.mark.parametrize("lost_response", ["add", "move"])
def test_recovery_after_lost_response_reuses_destination_and_does_not_duplicate(transfer, lost_response):
    t = transfer
    _run(t)
    t[3].failed_title = None
    t[3].lost_response = lost_response
    assert _run(t, 101)["playlists_failed"] == 1
    assert not _state(t)["complete"]
    assert _run(t, 102)["playlists_imported"] == 1
    assert _order(t) == ["yt-One", "yt-Two", "yt-Three"]
    assert len(t[3].created) == 1
    assert Counter(video for _, batch in t[3].added for video in batch) == {
        "yt-One": 1, "yt-Two": 1, "yt-Three": 1}
    assert _state(t)["complete"]


def test_destination_additions_and_duplicate_occurrences_keep_their_slots(transfer):
    t = transfer
    _run(t)
    client = t[3]
    remote = client.created[0][0]
    tracks = client._tracks[remote]
    client._tracks[remote] = [_track("user-x", "X", "User", set_video_id="x-first"), tracks[0],
        _track("user-y", "Y", "User", set_video_id="y"), tracks[1],
        _track("user-x", "X", "User", set_video_id="x-second"),
        _track("user-z", "Z", "User", set_video_id="z")]
    client.failed_title = None
    assert _run(t, 101)["playlists_imported"] == 1
    assert _order(t) == ["user-x", "yt-One", "user-y", "yt-Two", "user-x", "user-z", "yt-Three"]
    assert not client.removed and not client.deleted
    assert all(kwargs["moveItem"][0].startswith("set-yt-")
               for _, kwargs in client.edited if isinstance(kwargs["moveItem"], tuple))


def test_previous_source_positions_survive_a_different_failed_lookup_on_retry(transfer):
    t = transfer
    t[3].failed_title = "Two"
    _run(t)
    t[3].failed_title = "One"
    assert _run(t, 101)["playlists_failed"] == 1
    assert _order(t) == ["yt-One", "yt-Two", "yt-Three"]
    assert not _state(t)["complete"]
    t[3].failed_title = None
    assert _run(t, 102)["playlists_imported"] == 1
    assert len(t[3].created) == 1


def test_older_completion_marker_is_rechecked_for_order(transfer):
    t = transfer
    _run(t)
    client = t[3]
    client.failed_title = None
    # Older versions appended recovered songs and marked them complete without verifying order.
    client.add_playlist_items(client.created[0][0], [client.search("One Artist")[0]["videoId"]])
    key = f"spotify_library_imported_playlists:{t[1]}"
    states = json.loads(t[0].settings.get_setting(key))
    old = next(iter(states.values()))
    old["complete"] = True
    old.pop("order_verified")
    old.pop("source_video_ids")
    t[0].settings.set_setting(key, json.dumps(states))
    assert _run(t, 101)["playlists_imported"] == 1
    assert _order(t) == ["yt-One", "yt-Two", "yt-Three"]
    assert len(client.created) == 1


@pytest.mark.parametrize("problem", ["handles", "silent-move", "truncated-read"])
def test_unresolved_remote_order_stays_retryable(transfer, monkeypatch, problem):
    t = transfer
    _run(t)
    client = t[3]
    client.failed_title = None
    read = client.get_playlist

    def broken_read(*args, **kwargs):
        detail = read(*args, **kwargs)
        if problem == "handles":
            return {**detail, "tracks": [{**track, "setVideoId": None} for track in detail["tracks"]]}
        return {**detail, "trackCount": len(detail["tracks"]) + 1}

    if problem == "silent-move":
        client.reject_move = True
    else:
        monkeypatch.setattr(client, "get_playlist", broken_read)
    assert _run(t, 101)["playlists_failed"] == 1
    assert not _state(t)["complete"]
    monkeypatch.setattr(client, "get_playlist", read)
    client.reject_move = False
    assert _run(t, 102)["playlists_imported"] == 1
    assert _order(t) == ["yt-One", "yt-Two", "yt-Three"]
    assert len(client.created) == 1
