"""The forward merge uses the editor's sequence and verifies it before deletion."""
import json
import re

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient, _track
from yt_playlist.library import executor, undo_capture
from yt_playlist.library.playlist_undo import undo_action
from yt_playlist.web.app import create_app


class MergeClient(FakeClient):
    def add_playlist_items(self, playlistId, videoIds, *, duplicates=False):
        start = len(self._tracks.get(playlistId, []))
        result = super().add_playlist_items(playlistId, videoIds, duplicates=duplicates)
        tracks = self._tracks[playlistId]
        for index in range(start, len(tracks)):
            # Remote handles identify occurrences, including a repeated song restored by Undo.
            tracks[index] = {**tracks[index], "setVideoId": f"{playlistId}-added-{len(self.added)}-{index}"}
        result["playlistEditResults"] = [
            {"videoId": track["videoId"], "setVideoId": track["setVideoId"]}
            for track in tracks[start:]]
        return result


@pytest.fixture
def merge(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    monkeypatch.setattr(executor, "_capture_post", lambda *a, **kw:
                        undo_capture.capture_post(*a, **kw, sleep=lambda _: None))
    iid = store.identities.upsert_identity("Main", "bridge", None, True)

    def setup(before, source):
        keep = store.playlists.upsert_playlist(iid, "KEEP", "Keeper", len(before), "", 1)
        other = store.playlists.upsert_playlist(iid, "SOURCE", "Source", len(source), "", 1)
        for pid, videos in ((keep, before), (other, source)):
            tids = [store.tracks.upsert_track(v, v.upper(), "Artist", None, 200) for v in videos]
            store.playlists.set_playlist_tracks(pid, list(dict.fromkeys(tids)))
        client = MergeClient(tracks={name: [_track(v, v.upper(), "Artist", set_video_id=f"{name}-{i}")
                                           for i, v in enumerate(videos)]
                                    for name, videos in (("KEEP", before), ("SOURCE", source))})
        return iid, keep, other, client

    return setup


@pytest.mark.parametrize("before,desired", [
    (["b", "a"], ["a", "b"]),
    (["a", "c"], ["a", "b", "c"]),
    (["d", "b", "a"], ["a", "b", "c", "d"]),
    (["b", "a", "b"], ["a", "b"]),
])
def test_merge_applies_exact_order_and_saves_verified_undo(merge, store, before, desired):
    iid, keep, other, client = merge(before, desired)
    result = executor.apply_result(store, {iid: client}, [keep, other], desired, keep, 2)
    assert [t["videoId"] for t in client._tracks["KEEP"]] == desired
    assert result["deleted"] == ["Source"]
    action = store.actions.get_actions()[0]
    entry = json.loads(action.undo_json)["restored"][0]
    assert entry["prev"] == before and entry["post"] == desired
    assert entry["post_state"]["videos"] == desired
    undo_action(store, action.id, {iid: client}, 3)
    assert [t["videoId"] for t in client._tracks["KEEP"]] == before


@pytest.mark.parametrize("response", [False, "STATUS_FAILED", None, "STATUS_SUCCEEDED"])
def test_rejected_or_silent_moves_cannot_publish_success_or_delete_sources(merge, store, monkeypatch, response):
    iid, keep, other, client = merge(["d", "c", "b", "a"], ["a", "b", "c", "d"])

    def no_move(playlist, **kwargs):
        client.edited.append((playlist, kwargs))
        return response

    monkeypatch.setattr(client, "edit_playlist", no_move)
    with pytest.raises(ValueError, match="position change|verify the merged playlist order"):
        executor.apply_result(store, {iid: client}, [keep, other], ["a", "b", "c", "d"], keep, 2)
    assert not client.deleted and store.playlists.get_playlist(other) is not None
    action = store.actions.get_actions()[0]
    assert json.loads(action.params_json)["partial"]
    assert json.loads(action.undo_json)["restored"][0]["post_state"] is None
    if response != "STATUS_SUCCEEDED":
        assert len(client.edited) == 1


def test_ordering_uses_handles_from_post_membership_readback(merge, store, monkeypatch):
    iid, keep, other, client = merge(["a", "c"], ["a", "b", "c"])
    read = client.get_playlist
    refreshed = False

    def current_handles(playlist, **kwargs):
        nonlocal refreshed
        if playlist == "KEEP" and client.added and not refreshed:
            for index, track in enumerate(client._tracks["KEEP"]):
                track["setVideoId"] = f"current-{index}"
            refreshed = True
        return read(playlist, **kwargs)

    monkeypatch.setattr(client, "get_playlist", current_handles)
    executor.apply_result(store, {iid: client}, [keep, other], ["a", "b", "c"], keep, 2)
    assert client.edited
    for _, kwargs in client.edited:
        move = kwargs["moveItem"]
        assert all(handle.startswith("current-") for handle in (move if isinstance(move, tuple) else [move]))


def test_skipped_addition_keeps_confirmed_order_but_cannot_authorize_source_deletion(merge, store, monkeypatch):
    iid, keep, other, client = merge(["b", "a"], ["a", "missing", "b"])
    monkeypatch.setattr(client, "add_playlist_items", lambda *a, **kw: {"status": "STATUS_FAILED"})
    with pytest.raises(ValueError, match="missing 1 required"):
        executor.apply_result(store, {iid: client}, [keep, other], ["a", "missing", "b"], keep, 2)
    assert [t["videoId"] for t in client._tracks["KEEP"]] == ["a", "b"]
    assert not client.deleted
    action = store.actions.get_actions()[0]
    assert json.loads(action.params_json)["skipped"] == 1
    assert json.loads(action.undo_json)["restored"][0]["post"] == ["a", "b"]


@pytest.mark.parametrize("sort,mode,expected", [
    ("alpha", "interleaved", ["a", "b", "c"]),
    ("alpha", "ducks", ["b", "a", "c"]),
    ("playlist", "interleaved", ["c", "b", "a"]),
    ("playlist", "ducks", ["b", "c", "a"]),
])
def test_apply_matches_the_rendered_editor_for_every_ordering_mode(merge, store, sort, mode, expected):
    iid, keep, other, client = merge(["b", "a"], ["c", "b"])
    app = create_app(store, lambda: {iid: client}, now_fn=lambda: 2, background_work=False)
    with TestClient(app, base_url="http://127.0.0.1") as web:
        ids = f"{keep},{other}"
        assert web.get(f"/merge?ids={ids}").status_code == 200
        for field, value in (("sort", sort), ("mode", mode)):
            preview = web.post(f"/merge/update?ids={ids}", data={"field": field, "value": value})
        shown = re.findall(r'href="https://music.youtube.com/watch\?v=([^"]+)"', preview.text)
        assert shown == expected
        response = web.post(f"/merge/apply?ids={ids}")
        assert response.status_code == 200 and response.headers.get("HX-Redirect")
        assert [t["videoId"] for t in client._tracks["KEEP"]] == shown
        assert store.actions.get_draft(ids) is None
