"""Deletion recovery must describe remote contents, never the last local sync."""
import json
import os
import stat
from pathlib import Path

import pytest

from tests.conftest import FakeClient, _track
from yt_playlist.library import executor, playlist_undo, playlist_backups, playlist_generated
from yt_playlist.repos.base import GENERATED_GROUP


@pytest.fixture
def source(store, tmp_path, monkeypatch):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    monkeypatch.setattr(executor, "with_retry", lambda read: read())
    monkeypatch.setattr(playlist_backups, "with_retry", lambda read: read())
    iid = store.accounts.upsert_account("Main", "bridge", None, True)
    pid = store.playlists.upsert_playlist(iid, "SOURCE", "Cached title", 1, "", 1)
    tid = store.tracks.upsert_track("one", "Cached song", "Artist", None, 200)
    store.playlists.set_playlist_tracks(pid, [tid])
    tracks = [_track("new", "Added remotely", "Artist", set_video_id="new-first"),
              _track("one", "Current title", "Artist", set_video_id="one"),
              _track("new", "Added remotely", "Artist", set_video_id="new-second")]
    client = FakeClient(tracks={"SOURCE": tracks, "KEEP": list(tracks)})
    return iid, pid, client, tracks


@pytest.mark.parametrize("operation", ["manual", "merge", "move", "duplicate", "gc"])
def test_every_deletion_path_backs_up_current_order_and_occurrences(store, source, tmp_path, operation):
    iid, pid, client, tracks = source
    if operation == "manual":
        executor.delete_playlist(store, pid, client, 1000000)
    elif operation == "gc":
        store.playlists.set_playlist_group("SOURCE", GENERATED_GROUP)
        store.playlists.mark_generated_playlist('SOURCE')
        assert playlist_generated.gc_generated_playlists(store, {iid: client}, 1000000, grace_days=1)
    elif operation == "move":
        # A move knows the two songs locally, but its cached order lacks the remote repeat.
        new = store.tracks.upsert_track("new", "Added remotely", "Artist", None, 200)
        one = store.tracks.upsert_track("one", "Cached song", "Artist", None, 200)
        store.playlists.set_playlist_tracks(pid, [one, new])
        target = store.accounts.upsert_account("Other", "bridge", None, False)
        destination = FakeClient(catalog=client._catalog)
        result = executor.copy_or_move_playlist(store, pid, target, client, destination,
                                                1000000, delete_source=True)
        assert result["deleted"]
    else:
        keep = store.playlists.upsert_playlist(iid, "KEEP", "Kept", 3, "", 1)
        if operation == "merge":
            executor.apply_result(store, {iid: client}, [keep, pid], ["new", "one"], keep, 1000000)
        else:
            aid = executor.store_plan(store, executor.MergePlan(pid, keep, [], []),
                                      "delete", "SOURCE", 1)
            executor.execute_planned(store, aid, {iid: client}, 1000000)
    backup, = (tmp_path / "backups").glob("SOURCE_*.json")
    from yt_playlist.core import fao
    saved = json.loads(fao.read_text(backup))
    assert [t["video_id"] for t in saved["tracks"]] == ["new", "one", "new"]
    assert [t["set_video_id"] for t in saved["tracks"]] == ["new-first", "one", "new-second"]
    assert [t["position"] for t in saved["tracks"]] == [0, 1, 2]
    assert [t["remote"] for t in saved["tracks"]] == tracks
    assert saved["identity_id"] == iid
    assert "SOURCE" in client.deleted
    assert store.playlists.get_playlist(pid) is None


def test_undo_uses_the_remote_snapshot_including_unseen_additions(store, source):
    iid, pid, client, _ = source
    executor.delete_playlist(store, pid, client, 2)
    action, = store.actions.get_actions()
    playlist_undo.undo_action(store, action.id, {iid: client}, 3)
    assert client.added[-1] == ("PL_NEW_0", ["new", "one", "new"])
    assert [t["videoId"] for t in client._tracks["PL_NEW_0"]] == ["new", "one", "new"]


@pytest.mark.parametrize("detail", [
    {}, {"tracks": None}, {"tracks": [], "trackCount": 3},
    {"tracks": [], "continuation": "next"}, {"tracks": [], "continuations": ["next"]},
    {"tracks": [], "status": "STATUS_FAILED"}, {"tracks": [], "error": "denied"},
    {"tracks": [{"title": "Unavailable"}]}, {"tracks": [{"videoId": 123}]},
])
def test_incomplete_remote_read_preserves_playlist_and_publishes_nothing(
        store, source, tmp_path, monkeypatch, detail):
    _, pid, client, _ = source
    monkeypatch.setattr(client, "get_playlist", lambda *a, **kw: detail)
    with pytest.raises(ValueError):
        executor.delete_playlist(store, pid, client, 2)
    assert not client.deleted
    assert store.playlists.get_playlist(pid) is not None
    assert store.actions.get_actions() == []
    assert not list(tmp_path.glob("backups/*"))


@pytest.mark.parametrize("failure", ["fetch", "write", "publish", "fsync", "file_fsync",
                                     pytest.param("directory_fsync", marks=pytest.mark.skipif(
                                         os.name == "nt", reason="POSIX directory durability"))])
def test_failed_backup_does_not_delete_or_overwrite_prior_recovery(
        store, source, tmp_path, monkeypatch, failure):
    _, pid, client, _ = source
    prior = Path(playlist_backups.backup_playlist(store, pid, 2, client))
    saved = prior.read_bytes()
    boundary_reached = []

    def fail(*args, **kwargs):
        raise OSError("Backup unavailable")

    if failure == "fetch":
        monkeypatch.setattr(client, "get_playlist", fail)
    elif failure == "write":
        monkeypatch.setattr(playlist_backups.json, "dumps", fail)
    elif failure == "publish":
        monkeypatch.setattr(playlist_backups.os, "replace", fail)
    elif failure == "fsync":
        monkeypatch.setattr(playlist_backups.os, "fsync", fail)
    else:
        fsync = playlist_backups.os.fsync

        def fail_at_boundary(fd):
            info = os.fstat(fd)
            if failure == "file_fsync" and stat.S_ISREG(info.st_mode):
                boundary_reached.append(failure)
                fail()
            if (failure == "directory_fsync" and stat.S_ISDIR(info.st_mode)
                    and os.path.samestat(info, prior.parent.stat())
                    and len(list(prior.parent.glob("SOURCE_*.json"))) == 2):
                boundary_reached.append(failure)
                fail()
            return fsync(fd)

        monkeypatch.setattr(playlist_backups.os, "fsync", fail_at_boundary)
    with pytest.raises(OSError, match="Backup unavailable"):
        executor.delete_playlist(store, pid, client, 2)
    if failure in {"file_fsync", "directory_fsync"}:
        assert boundary_reached == [failure]
    assert prior.read_bytes() == saved
    retained = set(prior.parent.iterdir())
    if failure == "directory_fsync":
        # The atomic rename completed, but its directory entry is not yet durable.
        assert len(retained - {prior, prior.parent / ".fao.lock"}) == 1
        assert len(list(prior.parent.glob("SOURCE_*.json"))) == 2
    else:
        assert retained == {prior, prior.parent / ".fao.lock"}
    assert not list(prior.parent.glob(".fao-pending-*"))
    assert not client.deleted
    assert store.playlists.get_playlist(pid) is not None
    assert store.actions.get_actions() == []
    if failure in {"file_fsync", "directory_fsync"}:
        monkeypatch.setattr(playlist_backups.os, "fsync", fsync)
        executor.delete_playlist(store, pid, client, 3)
        assert client.deleted == ["SOURCE"]
        assert store.actions.get_actions()[0].status == "executed"
        assert prior.read_bytes() == saved


def test_merge_gate_and_backup_use_the_same_source_snapshot(store, source, monkeypatch):
    iid, pid, client, tracks = source
    keep = store.playlists.upsert_playlist(iid, "KEEP", "Kept", 3, "", 1)
    reads = []
    get = client.get_playlist

    def read(playlist, limit=None):
        if playlist in client.deleted:
            return get(playlist, limit)  # deletion confirmation is a separate absence read
        if playlist == "SOURCE":
            reads.append(playlist)
            if len(reads) > 1:
                return {"tracks": [*tracks, _track("later", "Changed again", "Artist")]}
        return get(playlist, limit)

    monkeypatch.setattr(client, "get_playlist", read)
    aid = executor.store_plan(store, executor.MergePlan(pid, keep, [], []), "delete", "SOURCE", 1)
    executor.execute_planned(store, aid, {iid: client}, 2)
    assert reads == ["SOURCE"]
    from yt_playlist.core import fao
    payload = json.loads(fao.read_text(Path(json.loads(store.actions.get_action(aid).undo_json)["backup"])))
    assert [t["video_id"] for t in payload["tracks"]] == ["new", "one", "new"]
