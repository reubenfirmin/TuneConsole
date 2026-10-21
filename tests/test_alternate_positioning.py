"""#40: alternate placement must survive YouTube indexing lag and failed moves."""
import json

import pytest

from tests.conftest import FakeClient, _track
from yt_playlist.core.store import Store
from yt_playlist.library import executor, sync


class LaggingClient(FakeClient):
    hidden_reads = 0
    return_handles = True
    fail_moves = False

    def get_playlist(self, playlistId, limit=100):
        result = super().get_playlist(playlistId, limit)
        if self.hidden_reads:
            self.hidden_reads -= 1
            result["tracks"] = [t for t in result["tracks"] if not t["videoId"].startswith("alt")]
        return result

    def add_playlist_items(self, playlistId, videoIds):
        result = super().add_playlist_items(playlistId, videoIds)
        return result if self.return_handles else None

    def edit_playlist(self, playlistId, **kw):
        if self.fail_moves:
            raise RuntimeError("YouTube temporarily rejected the move")
        return super().edit_playlist(playlistId, **kw)


def seed(store):
    iid = store.upsert_identity("main", "cred", None, True)
    tracks = [_track(vid, title, "Artist", set_video_id="set_" + vid)
              for vid, title in (("anchor", "Original"), ("next", "Next"),
                                 ("alt1", "Alternate one"), ("alt2", "Alternate two"))]
    tids = [store.upsert_track(t["videoId"], t["title"], "Artist", "Alb", 200) for t in tracks[:2]]
    pid = store.upsert_playlist(iid, "PL", "Mix", 2, "hash", 1)
    store.set_playlist_tracks(pid, tids)
    client = LaggingClient(playlists=[{"playlistId": "PL", "title": "Mix"}],
                           tracks={"PL": tracks[:2]}, catalog={t["videoId"]: t for t in tracks})
    chosen = [{"videoId": t["videoId"], "title": t["title"], "artist": "Artist", "duration": 200}
              for t in tracks[2:]]
    return iid, pid, client, chosen


def local(store, pid):
    return [t["video_id"] for t in store.playlist_tracks_detail(pid)]


def remote(client):
    return [t["videoId"] for t in client._tracks["PL"]]


@pytest.mark.parametrize("return_handles", [True, False])
def test_add_positions_multiple_alternates_despite_lag(store, return_handles):
    _, pid, client, chosen = seed(store)
    client.return_handles = return_handles
    client.hidden_reads = 1 if not return_handles else 100
    result = executor.add_tracks_to_playlist(store, pid, chosen, client, 100, after_video_id="anchor")
    assert result["position_pending"] == 0
    assert local(store, pid) == remote(client) == ["anchor", "alt1", "alt2", "next"]
    assert store.get_actions()[0].status == "executed"
    assert client.added == [("PL", ["alt1", "alt2"])]


def test_pending_move_survives_restart_and_sync_without_adding_again(tmp_path):
    path = str(tmp_path / "library.db")
    store = Store(path)
    store.init_schema()
    iid, pid, client, chosen = seed(store)
    client.return_handles = False
    client.hidden_reads = 100
    result = executor.add_tracks_to_playlist(store, pid, chosen, client, 100, after_video_id="anchor")
    assert result["position_pending"] == 1
    assert local(store, pid) == ["anchor", "alt1", "alt2", "next"]
    assert remote(client) == ["anchor", "next", "alt1", "alt2"]
    action = store.get_actions()[0]
    assert action.status == "pending"
    assert json.loads(action.plan_json)["after_video_id"] == "anchor"
    store.conn.close()

    store = Store(path)
    store.init_schema()
    # A sync during the lag must preserve the requested tracks and their pending move.
    sync.sync_identity(store, iid, client, 101)
    assert local(store, pid) == ["anchor", "alt1", "alt2", "next"]
    assert store.get_action(action.id).status == "pending"
    client.hidden_reads = 0
    sync.sync_identity(store, iid, client, 102)
    assert local(store, pid) == remote(client) == ["anchor", "alt1", "alt2", "next"]
    assert store.get_action(action.id).status == "executed"
    sync.refresh_playlist(store, iid, client, "PL", "Mix", 103)
    assert local(store, pid) == remote(client)
    assert client.added == [("PL", ["alt1", "alt2"])]
    store.conn.close()


def test_failed_move_remains_pending_until_targeted_refresh(store):
    iid, pid, client, chosen = seed(store)
    client.fail_moves = True
    result = executor.add_tracks_to_playlist(store, pid, chosen, client, 100, after_video_id="anchor")
    assert result["position_pending"] == 1
    assert store.get_actions()[0].status == "pending"
    client.fail_moves = False
    sync.refresh_playlist(store, iid, client, "PL", "Mix", 101)
    assert local(store, pid) == remote(client) == ["anchor", "alt1", "alt2", "next"]
    assert store.get_actions()[0].status == "executed"
    assert client.added == [("PL", ["alt1", "alt2"])]


def test_missing_successor_handle_does_not_move_alternates_to_end(store):
    _, pid, client, chosen = seed(store)
    client._tracks["PL"][1].pop("setVideoId")
    result = executor.add_tracks_to_playlist(store, pid, chosen, client, 100, after_video_id="anchor")
    assert result["position_pending"] == 1
    assert client.edited == []
    assert store.get_actions()[0].status == "pending"


def test_last_track_anchor_needs_no_move(store):
    _, pid, client, chosen = seed(store)
    result = executor.add_tracks_to_playlist(store, pid, chosen, client, 100, after_video_id="next")
    assert result["position_pending"] == 0
    assert local(store, pid) == remote(client) == ["anchor", "next", "alt1", "alt2"]
    assert client.edited == []


@pytest.mark.parametrize("edit", ["remove", "reorder"])
def test_later_explicit_edit_cancels_pending_placement(store, edit):
    iid, pid, client, chosen = seed(store)
    client.fail_moves = True
    executor.add_tracks_to_playlist(store, pid, chosen, client, 100, after_video_id="anchor")
    action = store.get_actions()[0]
    assert action.status == "pending"
    client.fail_moves = False
    if edit == "remove":
        executor.remove_track(store, pid, "alt1", client, 101)
        expected = ["anchor", "next", "alt2"]
    else:
        executor.reorder_track(store, pid, "alt1", "anchor", client, 101)
        expected = ["alt1", "anchor", "next", "alt2"]
    sync.refresh_playlist(store, iid, client, "PL", "Mix", 102)
    assert local(store, pid) == remote(client) == expected
    assert store.get_action(action.id).status == "cancelled"
