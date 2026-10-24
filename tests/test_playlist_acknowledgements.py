"""Local playlist success requires an acknowledgement or an exact bounded readback."""
import json

import pytest

from tests.test_undo_recovery import RecoveryClient
from yt_playlist.library import executor, playlist_mutation
from yt_playlist.library.playlist_deletion import reconcile_deletion


class MutationClient(RecoveryClient):
    def __init__(self, response, *, apply=True, lose_reply=False, unreadable=False):
        super().__init__({"SOURCE": ["a", "b", "c"]})
        self.response = response
        self.apply = apply
        self.lose_reply = lose_reply
        self.unreadable = unreadable
        self.writes = 0

    def _write(self, apply):
        self.writes += 1
        if self.apply:
            apply()
        if self.lose_reply:
            raise TimeoutError("Reply lost")
        return self.response

    def edit_playlist(self, playlist_id, **kwargs):
        def apply():
            super(MutationClient, self).edit_playlist(playlist_id, **kwargs)
            if "title" in kwargs:
                self.metadata[playlist_id]["title"] = kwargs["title"]
        return self._write(apply)

    def remove_playlist_items(self, playlist_id, tracks):
        return self._write(lambda: super(MutationClient, self).remove_playlist_items(playlist_id, tracks))

    def delete_playlist(self, playlist_id):
        return self._write(lambda: super(MutationClient, self).delete_playlist(playlist_id))

    def get_playlist(self, playlist_id, limit=None):
        if self.writes and self.unreadable:
            raise ConnectionError("Readback unavailable")
        return super().get_playlist(playlist_id, limit)


@pytest.fixture
def playlist(store, tmp_path, monkeypatch):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    # Controlled readback failures need no transport backoff in this regression test.
    monkeypatch.setattr(executor, "with_retry", lambda read: read())
    monkeypatch.setattr(playlist_mutation, "with_retry", lambda read: read())
    identity = store.identities.upsert_identity("Main", "bridge", None, True)
    pid = store.playlists.upsert_playlist(identity, "SOURCE", "Source", 3, "", 1)
    tids = [store.tracks.upsert_track(video, video, "Artist", None, 200) for video in ("a", "b", "c")]
    store.playlists.set_playlist_tracks(pid, tids)
    return identity, pid, tids


def _run(operation, store, playlist, client):
    _, pid, _ = playlist
    if operation == "rename":
        return executor.rename_playlist(store, pid, "New title", client, 2)
    if operation == "remove":
        return executor.remove_track(store, pid, "a", client, 2)
    if operation == "reorder":
        return executor.reorder_track(store, pid, "c", "a", client, 2)
    return executor.delete_playlist(store, pid, client, 2)


def _assert_success(operation, store, playlist):
    _, pid, tids = playlist
    if operation == "rename":
        assert store.playlists.get_playlist(pid).title == "New title"
    elif operation == "remove":
        assert store.playlists.get_playlist_track_ids(pid) == tids[1:]
    elif operation == "reorder":
        assert store.playlists.get_playlist_track_ids(pid) == [tids[2], *tids[:2]]
    else:
        assert store.playlists.get_playlist(pid) is None
        assert store.actions.get_actions()[0].status == "executed"


@pytest.mark.parametrize("operation", ["rename", "remove", "reorder", "delete"])
@pytest.mark.parametrize("response", ["STATUS_SUCCEEDED", {"status": "STATUS_SUCCEEDED"}, None, {}])
def test_string_dict_and_missing_acknowledgements_confirm_success(store, playlist, operation, response):
    client = MutationClient(response)
    _run(operation, store, playlist, client)
    _assert_success(operation, store, playlist)
    assert client.writes == 1


@pytest.mark.parametrize("operation", ["rename", "remove", "reorder", "delete"])
@pytest.mark.parametrize("response", [False, "STATUS_FAILED", {"status": "STATUS_FAILED"}, {"error": "Rejected"}])
def test_explicit_failure_never_publishes_local_success(store, playlist, operation, response):
    client = MutationClient(response, apply=False)
    with pytest.raises(ValueError, match="rejected"):
        _run(operation, store, playlist, client)
    _, pid, tids = playlist
    assert store.playlists.get_playlist(pid).title == "Source"
    assert store.playlists.get_playlist_track_ids(pid) == tids
    assert client.writes == 1
    if operation == "delete":
        action, = store.actions.get_actions()
        assert action.status == "delete_failed" and json.loads(action.undo_json)["backup"]
    else:
        assert store.actions.get_actions() == []


@pytest.mark.parametrize("operation", ["rename", "remove", "reorder"])
def test_lost_reply_after_commit_is_read_back_without_replaying(store, playlist, operation):
    client = MutationClient(None, lose_reply=True)
    _run(operation, store, playlist, client)
    _assert_success(operation, store, playlist)
    assert client.writes == 1


@pytest.mark.parametrize("operation", ["rename", "remove", "reorder"])
@pytest.mark.parametrize("failure", ["missing_no_effect", "lost_before_commit", "unreadable_after_commit"])
def test_unconfirmed_write_preserves_local_state_and_is_not_replayed(store, playlist, operation, failure):
    client = MutationClient(None, apply=failure == "unreadable_after_commit",
                            lose_reply=failure == "lost_before_commit",
                            unreadable=failure == "unreadable_after_commit")
    with pytest.raises(ValueError, match="unconfirmed"):
        _run(operation, store, playlist, client)
    _, pid, tids = playlist
    assert store.playlists.get_playlist(pid).title == "Source"
    assert store.playlists.get_playlist_track_ids(pid) == tids
    assert store.actions.get_actions() == [] and client.writes == 1


def test_lost_delete_reply_retains_pending_action_then_reconciles_without_replay(store, playlist):
    client = MutationClient(None, lose_reply=True)
    with pytest.raises(TimeoutError):
        _run("delete", store, playlist, client)
    identity, pid, _ = playlist
    action, = store.actions.get_actions()
    assert action.status == "delete_pending" and store.playlists.get_playlist(pid) is not None
    reconcile_deletion(store, action.id, {identity: client}, 3)
    _assert_success("delete", store, playlist)
    assert client.writes == 1


@pytest.mark.parametrize("response", [None, {}])
def test_unacknowledged_delete_that_did_not_commit_keeps_local_playlist(store, playlist, response):
    client = MutationClient(response, apply=False)
    with pytest.raises(ValueError, match="not confirmed"):
        _run("delete", store, playlist, client)
    assert store.playlists.get_playlist(playlist[1]) is not None
    assert store.actions.get_actions()[0].status == "delete_failed" and client.writes == 1


def test_missing_reorder_destination_cannot_turn_into_move_to_end(store, playlist):
    client = MutationClient("STATUS_SUCCEEDED")
    with pytest.raises(ValueError, match="destination track"):
        executor.reorder_track(store, playlist[1], "a", "missing", client, 2)
    assert client.writes == 0 and store.playlists.get_playlist_track_ids(playlist[1]) == playlist[2]
