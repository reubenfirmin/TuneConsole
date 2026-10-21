from types import SimpleNamespace
import pytest

from yt_playlist.library import playlist_mutation
from yt_playlist.library.playlist_mutation import PlaylistResult, reconcile_playlist
from yt_playlist.rec import radio as radio_mod
from yt_playlist.web.radio_controller import RadioController


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


def controller_for(remote):
    session = radio_mod.RadioSession()
    session.active = True
    session.playlist_ytm = "PL"
    sent = []
    ctx = SimpleNamespace(radio=session, bridge=SimpleNamespace(send_control=sent.append),
                          store=None, client_provider=lambda: {"id": remote})
    return RadioController(ctx), session, sent


def test_partial_apply_commits_only_confirmed_tracks_then_retries_identical_plan():
    remote = RemotePlaylist(["a"], unavailable=["b"])
    controller, session, sent = controller_for(remote)
    plan = {"desired_vids": ["a", "b"], "prime": {"video_id": "b"}}
    result = controller.apply(session, plan, session.start_gen)
    assert result.status == "partial"
    assert session.applied_vids == ["a"]
    assert session.apply_result["skipped"] == ["b"]
    assert sent == []
    remote.unavailable.clear()
    assert controller.apply(session, plan, session.start_gen).complete
    assert session.applied_vids == ["a", "b"]
    assert sent[-1]["videoId"] == "b"


def test_stop_during_apply_drops_commit_and_navigation(monkeypatch):
    remote = RemotePlaylist(["a"])
    controller, session, sent = controller_for(remote)
    generation = session.start_gen
    add = remote.add_playlist_items

    def stop_during_add(*args):
        controller.stop(send=False)
        return add(*args)

    monkeypatch.setattr(remote, "add_playlist_items", stop_during_add)
    controller.apply(session, {"desired_vids": ["a", "b"], "prime": {"video_id": "b"}}, generation)
    assert not session.active
    assert session.applied_vids == []
    assert sent == []
    # A queued apply from before stop cannot overwrite the new session, even at epoch zero.
    session.active = True
    session.playlist_ytm = "PL"
    assert controller.apply(session, {"desired_vids": ["old"]}, generation) is None
    assert remote.vids == ["a", "b"]


@pytest.mark.parametrize("catalog_exhausted", [False, True])
def test_partial_deck_remains_dirty_and_unchanged_queue_can_be_retried(monkeypatch, catalog_exhausted):
    remote = RemotePlaylist(["a"], unavailable=["b"])
    controller, session, sent = controller_for(remote)
    session.dual_deck = True
    picks = [{"key": v, "video_id": v, "artist": v} for v in ("a", "b")]
    deck = session.decks["B"]
    deck.update(playlist_ytm="PL", queue=picks[:], boundary_vid="b", applied_vids=["a"])
    plan = {"playlist_key": "B", "vids": ["a", "b"], "first": picks[0], "boundary": "b", "epoch": 0}
    assert controller.apply_deck(session, plan, session.start_gen).status == "partial"
    assert session.standby_dirty
    assert sent == []
    monkeypatch.setattr(radio_mod.rec_params, "get_param", lambda *args: 2)

    def same_picks(store, se, target, now, depth):
        target["queue"] = [] if catalog_exhausted else picks[:]
        target["boundary_vid"] = "b"

    monkeypatch.setattr(radio_mod, "_seed_deck", same_picks)
    retry = radio_mod.rebuild_standby(None, session, 100)
    assert retry is not None
    remote.unavailable.clear()
    assert controller.apply_deck(session, retry, session.start_gen).complete
    assert deck["applied_vids"] == ["a", "b"]
    assert sent[-1]["type"] == "deck-boundary-config"
