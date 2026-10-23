import json

import pytest

from tests.conftest import FakeClient, _track
from yt_playlist.core import paths
from yt_playlist.library.playlist_undo import undo_action
from yt_playlist.library.playlist_mutation import restore_playlist_sequence
from yt_playlist.util.action_kinds import APPLY_MERGE


@pytest.fixture(autouse=True)
def isolated_data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)


class OccurrenceClient(FakeClient):
    def __init__(self, vids):
        super().__init__(tracks={"KEEP": [_track(v, v, "Artist", set_video_id=f"item-{i}")
                                         for i, v in enumerate(vids)]})
        self.serial = len(vids)
        self.reject_moves = False

    def add_playlist_items(self, playlistId, videoIds, *, duplicates=False):
        self.added.append((playlistId, list(videoIds)))
        for vid in videoIds:
            self._tracks[playlistId].append(_track(vid, vid, "Artist", set_video_id=f"item-{self.serial}"))
            self.serial += 1
        return {"status": "STATUS_SUCCEEDED"}

    def remove_playlist_items(self, playlistId, videos):
        self.removed.append((playlistId, list(videos)))
        handles = {v["setVideoId"] for v in videos}
        self._tracks[playlistId] = [t for t in self._tracks[playlistId] if t["setVideoId"] not in handles]

    def edit_playlist(self, playlistId, **kwargs):
        if self.reject_moves:
            return "STATUS_FAILED"
        return super().edit_playlist(playlistId, **kwargs)


def _action(store, before, current):
    iid = store.identities.upsert_identity("Main", "bridge", None, True)
    pid = store.playlists.upsert_playlist(iid, "KEEP", "Kept playlist", len(current), "", 1)
    aid = store.actions.record_action(APPLY_MERGE, "{}", "{}", "executed", json.dumps({
        "restored": [{"ytm": "KEEP", "identity": iid, "prev": before, "post": current}],
    }), 1)
    return iid, pid, aid


@pytest.mark.parametrize("before,current", [
    (["a", "b", "c"], ["a", "c"]),
    (["a", "b", "c"], ["c", "a", "b"]),
    (["a", "b", "a"], ["a", "a"]),
    (["a", "b", "a"], ["a", "a", "b", "a"]),
])
def test_undo_restores_exact_order_and_refreshes_keeper(store, before, current):
    iid, pid, aid = _action(store, before, current)
    client = OccurrenceClient(current)
    undo_action(store, aid, {iid: client}, 2)
    assert [t["videoId"] for t in client._tracks["KEEP"]] == before
    # The local catalog displays each video once; recovery still preserves remote repeats.
    assert [row[1] for row in store.playlists.get_playlist_tracks_with_meta(pid)] == list(dict.fromkeys(before))
    assert store.actions.get_action(aid).status == "undone"


def test_rejected_positional_changes_remain_retryable(store):
    before, current = ["a", "b", "c"], ["c", "a", "b"]
    iid, _, aid = _action(store, before, current)
    client = OccurrenceClient(current)
    client.reject_moves = True
    with pytest.raises(ValueError, match="position change"):
        undo_action(store, aid, {iid: client}, 2)
    assert store.actions.get_action(aid).status != "undone"
    assert [t["videoId"] for t in client._tracks["KEEP"]] == current
    client.reject_moves = False
    undo_action(store, aid, {iid: client}, 3)
    assert [t["videoId"] for t in client._tracks["KEEP"]] == before


def test_silent_move_failure_cannot_report_recovery_complete(monkeypatch):
    client = OccurrenceClient(["c", "a", "b"])
    monkeypatch.setattr(client, "edit_playlist", lambda *a, **kw: "STATUS_SUCCEEDED")
    result = restore_playlist_sequence(client, "KEEP", ["a", "b", "c"])
    assert result.status == "partial"
    assert result.confirmed_vids == ("c", "a", "b")


def test_repeated_occurrence_removal_requires_a_remote_handle():
    client = OccurrenceClient(["a", "a"])
    for track in client._tracks["KEEP"]:
        track.pop("setVideoId")
    result = restore_playlist_sequence(client, "KEEP", ["a"])
    assert not result.complete
    assert not client.removed
    assert "removal handle" in result.error
