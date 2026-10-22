from yt_playlist.library import playlist_mutation
from yt_playlist.library.playlist_mutation import reconcile_playlist


class RemotePlaylist:
    def __init__(self, vids=(), unavailable=()):
        self.vids = list(vids)
        self.unavailable = set(unavailable)
        self.reads = 0

    def get_playlist(self, playlist, limit=None):
        self.reads += 1
        return {"tracks": [{"videoId": v, "setVideoId": "set-" + v} for v in self.vids],
                "trackCount": len(self.vids)}

    def add_playlist_items(self, playlist, vids):
        if self.unavailable.intersection(vids):
            raise ValueError("unavailable")
        self.vids.extend(vids)
        return {"status": "STATUS_SUCCEEDED"}

    def remove_playlist_items(self, playlist, tracks):
        for track in tracks:
            self.vids.remove(track["videoId"])

    def edit_playlist(self, playlist, moveItem):
        moved, successor = moveItem if isinstance(moveItem, tuple) else (moveItem, None)
        vid = moved.removeprefix("set-")
        self.vids.remove(vid)
        self.vids.insert(self.vids.index(successor.removeprefix("set-")) if successor else len(self.vids), vid)


def test_partial_add_is_verified_and_retry_restores_requested_order():
    remote = RemotePlaylist(["old", "c", "a"], unavailable=["b"])
    result = reconcile_playlist(remote, "PL", ["a", "b", "c"])
    assert result.status == "partial"
    assert result.confirmed_vids == ("a", "c")
    assert result.skipped == ("b",)
    remote.unavailable.clear()
    retry = reconcile_playlist(remote, "PL", ["a", "b", "c"])
    assert retry.complete
    assert retry.confirmed_vids == ("a", "b", "c")
    assert remote.vids == ["a", "b", "c"]


def test_success_response_cannot_hide_a_silent_remove_failure(monkeypatch):
    remote = RemotePlaylist(["a", "extra"])
    monkeypatch.setattr(remote, "remove_playlist_items", lambda *args: {"status": "STATUS_SUCCEEDED"})
    result = reconcile_playlist(remote, "PL", ["a"])
    assert result.status == "partial"
    assert "extra" in result.confirmed_vids


def test_unknown_final_state_is_failed_even_after_successful_add(monkeypatch):
    remote = RemotePlaylist()
    read = remote.get_playlist

    def failed_read(*args, **kwargs):
        if remote.reads >= 2:
            raise ValueError("connection lost")
        return read(*args, **kwargs)

    monkeypatch.setattr(remote, "get_playlist", failed_read)
    monkeypatch.setattr(playlist_mutation, "with_retry", lambda fn: fn())
    result = reconcile_playlist(remote, "PL", ["a"])
    assert remote.vids == ["a"]
    assert result.status == "failed"
    assert result.confirmed_vids is None
