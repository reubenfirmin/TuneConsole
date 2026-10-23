"""Shared move execution preserves each caller's occurrence and recovery contract."""
import pytest

from tests.conftest import FakeClient, _track
from yt_playlist.library.playlist_mutation import apply_playlist_permutation, restore_playlist_sequence
from yt_playlist.library.spotify_library import _order_imported_tracks


def _client(videos):
    return FakeClient(tracks={"PL": [_track(video, video, "Artist", set_video_id=f"item-{i}")
                                      for i, video in enumerate(videos)]})


@pytest.mark.parametrize("caller", ["undo", "import"])
@pytest.mark.parametrize("response", [None, {}, "STATUS_SUCCEEDED", {"status": "STATUS_SUCCEEDED"}])
@pytest.mark.parametrize("applied", [True, False])
def test_both_callers_require_confirmed_order(monkeypatch, caller, response, applied):
    client = _client(["c", "a", "b"])
    edit = client.edit_playlist

    def move(*args, **kwargs):
        if applied:
            edit(*args, **kwargs)
        else:
            client.edited.append((args[0], kwargs))
        return response

    monkeypatch.setattr(client, "edit_playlist", move)
    if caller == "undo":
        result = restore_playlist_sequence(client, "PL", ["a", "b", "c"])
        assert result.complete is applied
        assert result.confirmed_vids == (("a", "b", "c") if applied else ("c", "a", "b"))
    elif applied:
        assert [t["videoId"] for t in _order_imported_tracks(client, "PL", ["a", "b", "c"])] == ["a", "b", "c"]
    else:
        with pytest.raises(ValueError, match="position change|order is still unresolved"):
            _order_imported_tracks(client, "PL", ["a", "b", "c"])


@pytest.mark.parametrize("response", [False, "STATUS_FAILED", {"status": "STATUS_FAILED"}])
def test_rejection_stops_before_a_second_move(monkeypatch, response):
    client = _client(["a", "b", "c", "d"])
    moves = []
    monkeypatch.setattr(client, "edit_playlist", lambda *args, **kwargs: moves.append(kwargs) or response)
    with pytest.raises(ValueError, match="rejected a playlist position change"):
        apply_playlist_permutation(client, "PL", client._tracks["PL"], [2, 1, 0, 3])
    assert moves == [{"moveItem": ("item-0", "item-3")}]


def test_missing_acknowledgement_checks_duplicate_handles(monkeypatch):
    client = _client(["a", "b", "a"])
    tracks = client._tracks["PL"]

    def move_wrong_occurrence(*args, **kwargs):
        # The video IDs match the expected first move, but the wrong duplicate moved.
        client._tracks["PL"] = [tracks[1], tracks[0], tracks[2]]

    monkeypatch.setattr(client, "edit_playlist", move_wrong_occurrence)
    with pytest.raises(ValueError, match="did not confirm a playlist position change"):
        apply_playlist_permutation(client, "PL", tracks, [2, 1, 0])


def test_undo_restores_duplicate_occurrences_with_distinct_handles():
    client = _client(["b", "a", "a"])
    result = restore_playlist_sequence(client, "PL", ["a", "b", "a"])
    assert result.complete
    assert [track["setVideoId"] for track in client._tracks["PL"]] == ["item-1", "item-0", "item-2"]


def test_import_keeps_unmanaged_duplicates_in_their_slots():
    client = _client(["external", "c", "a", "a", "b"])
    confirmed = _order_imported_tracks(client, "PL", ["a", "b", "c"])
    assert [track["setVideoId"] for track in confirmed] == ["item-0", "item-2", "item-4", "item-3", "item-1"]
    moved = [kwargs["moveItem"] for _, kwargs in client.edited]
    assert moved == ["item-1", ("item-4", "item-3")]
    assert client.added == client.removed == []


@pytest.mark.parametrize("target", [[0, 0], [0], [0, 2]])
def test_invalid_targets_cannot_write(target):
    client = _client(["a", "b"])
    with pytest.raises(ValueError, match="each occurrence exactly once"):
        apply_playlist_permutation(client, "PL", client._tracks["PL"], target)
    assert client.edited == []
