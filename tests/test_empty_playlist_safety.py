"""Empty-only deletion requires current remote evidence, even when the cache is empty."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import FakeClient, _track
from yt_playlist.library import executor
from yt_playlist.util.retry import with_retry


@pytest.fixture
def empty_playlist(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    monkeypatch.setattr(executor, "with_retry", lambda fn: with_retry(fn, sleep=lambda _: None))
    iid = store.upsert_identity("Main", "bridge", None, True)
    pid = store.upsert_playlist(iid, "EMPTY", "Empty at last sync", 0, "", 1)
    return SimpleNamespace(store=store, pid=pid, client=FakeClient(), home=tmp_path)


def _assert_preserved(t):
    assert t.client.deleted == []
    assert t.store.get_playlist(t.pid) is not None
    assert t.store.get_actions() == []
    assert not list(t.home.glob("backups/*"))


@pytest.mark.parametrize("failure", [TimeoutError("timed out"), ConnectionError("disconnected"),
                                    PermissionError("HTTP 401"), KeyError("musicPlaylistShelfRenderer")],
                         ids=["timeout", "connection", "authentication", "empty-parser-error"])
def test_failed_read_cannot_delete_playlist_that_gained_remote_tracks(empty_playlist, monkeypatch, failure):
    t = empty_playlist
    # YouTube changed since sync, but the cached count still says zero.
    gained = _track("new", "Added elsewhere", "Artist")
    t.client._tracks["EMPTY"] = [gained]
    reads = []

    def unreadable(playlist, limit=None):
        reads.append((playlist, limit))
        raise failure

    monkeypatch.setattr(t.client, "get_playlist", unreadable)
    with pytest.raises(ValueError, match="Couldn't confirm.*empty on YouTube"):
        executor.delete_empty_playlist(t.store, t.pid, t.client, 2)
    assert reads == [("EMPTY", None)] * 3
    assert t.client._tracks["EMPTY"] == [gained]
    _assert_preserved(t)


@pytest.mark.parametrize("detail", [
    {}, {"tracks": None}, {"tracks": [], "error": "Unauthenticated"},
    {"tracks": [], "status": "STATUS_FAILED"}, {"tracks": [], "trackCount": 1},
    {"tracks": [], "continuation": "more"}, {"tracks": [{"title": "Unavailable"}]},
], ids=["missing-tracks", "invalid-tracks", "error", "failed-status", "truncated", "continuation",
        "unavailable-track"])
def test_uncertain_response_cannot_authorize_empty_deletion(empty_playlist, monkeypatch, detail):
    t = empty_playlist
    monkeypatch.setattr(t.client, "get_playlist", lambda playlist, limit=None: detail)
    with pytest.raises(ValueError, match="Couldn't confirm"):
        executor.delete_empty_playlist(t.store, t.pid, t.client, 2)
    _assert_preserved(t)


def test_remote_track_overrides_cached_emptiness(empty_playlist):
    t = empty_playlist
    t.client._tracks["EMPTY"] = [_track("new", "Added elsewhere", "Artist")]
    with pytest.raises(ValueError, match="isn't empty anymore"):
        executor.delete_empty_playlist(t.store, t.pid, t.client, 2)
    _assert_preserved(t)


@pytest.mark.parametrize("detail", [{"tracks": []}, {"tracks": [], "trackCount": 0}])
def test_confirmed_empty_response_still_backs_up_deletes_and_records_action(empty_playlist, monkeypatch, detail):
    t = empty_playlist
    monkeypatch.setattr(t.client, "get_playlist", lambda playlist, limit=None: detail)
    backup = executor.delete_empty_playlist(t.store, t.pid, t.client, 2)
    assert json.loads(Path(backup).read_text())["tracks"] == []
    assert t.client.deleted == ["EMPTY"]
    assert t.store.get_playlist(t.pid) is None
    action, = t.store.get_actions()
    assert action.kind == "delete_empty" and action.status == "executed"
    assert json.loads(action.undo_json)["backup"] == backup


@pytest.mark.parametrize("gained_tracks", [False, True])
def test_transient_failure_uses_successful_retry_to_decide(empty_playlist, monkeypatch, gained_tracks):
    t = empty_playlist
    reads = []
    tracks = [_track("new", "Added elsewhere", "Artist")] if gained_tracks else []

    def transient(playlist, limit=None):
        reads.append((playlist, limit))
        if len(reads) == 1:
            raise TimeoutError("temporary timeout")
        return {"tracks": tracks, "trackCount": len(tracks)}

    monkeypatch.setattr(t.client, "get_playlist", transient)
    if gained_tracks:
        with pytest.raises(ValueError, match="isn't empty anymore"):
            executor.delete_empty_playlist(t.store, t.pid, t.client, 2)
        _assert_preserved(t)
    else:
        executor.delete_empty_playlist(t.store, t.pid, t.client, 2)
        assert t.client.deleted == ["EMPTY"]
    assert reads == [("EMPTY", None)] * 2
