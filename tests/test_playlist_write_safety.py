"""Deleting a merge/move source requires fresh evidence of complete remote delivery."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import FakeClient, _track
from yt_playlist.library import executor


@pytest.fixture
def transfer(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    # Keep failing-read scenarios focused on the deletion decision, without retry delays.
    monkeypatch.setattr(executor, "with_retry", lambda fn: fn())
    source_identity = store.upsert_identity("Source", "bridge", None, True)
    target_identity = store.upsert_identity("Target", "bridge", None, False)
    tracks = [_track("v1", "One", "Artist"), _track("v2", "Two", "Artist")]
    tids = [store.upsert_track(t["videoId"], t["title"], "Artist", None, 200) for t in tracks]
    source = store.upsert_playlist(source_identity, "SOURCE", "Original", 2, "", 1)
    keeper = store.upsert_playlist(target_identity, "KEEP", "Keeper", 1, "", 1)
    store.set_playlist_tracks(source, tids)
    store.set_playlist_tracks(keeper, tids[:1])
    source_client = FakeClient(tracks={"SOURCE": tracks})
    target_client = FakeClient(tracks={"KEEP": tracks[:1]}, catalog={t["videoId"]: t for t in tracks})
    return SimpleNamespace(store=store, source=source, keeper=keeper, source_identity=source_identity,
                           target_identity=target_identity, source_client=source_client,
                           target_client=target_client, tracks=tracks, tids=tids,
                           clients={source_identity: source_client, target_identity: target_client})


def _run(t, operation):
    if operation == "merge":
        return executor.apply_result(t.store, t.clients, [t.keeper, t.source], ["v1", "v2"], t.keeper, 10)
    if operation == "move":
        return executor.copy_or_move_playlist(t.store, t.source, t.target_identity,
                                              t.source_client, t.target_client, 10, delete_source=True)
    aid = executor.store_plan(t.store, executor.MergePlan(t.source, t.keeper, [], []),
                              "delete", "SOURCE", 1)
    return executor.execute_planned(t.store, aid, t.clients, 10)


@pytest.mark.parametrize("operation", ["merge", "move"])
@pytest.mark.parametrize("failure", ["rejected", "failed_status", "missing_status", "timeout",
                                    "partial_delivery", "wrong_upload"])
def test_failed_delivery_never_deletes_source(transfer, monkeypatch, operation, failure):
    t = transfer
    add = t.target_client.add_playlist_items

    def fail_add(playlist, videos):
        if failure == "rejected":
            raise ValueError("Unavailable upload")
        if failure == "timeout":
            raise TimeoutError("Bridge response timed out")
        if failure == "failed_status":
            return {"status": "STATUS_FAILED"}
        if failure == "missing_status":
            return {}
        result = add(playlist, [v for v in videos if v != "v2"])
        if failure == "wrong_upload":
            # Matching title/artist does not establish preservation of the exact playable upload.
            t.target_client._tracks[playlist].append(_track("other-upload", "Two", "Artist"))
        return result

    monkeypatch.setattr(t.target_client, "add_playlist_items", fail_add)
    if operation == "merge":
        with pytest.raises(ValueError, match="Refusing to delete"):
            _run(t, operation)
        action = t.store.get_actions()[0]
        assert json.loads(action.params_json)["partial"]
        assert json.loads(action.undo_json)["restored"][0]["prev"] == ["v1"]
    else:
        result = _run(t, operation)
        assert not result["deleted"]
        assert result["skipped"] or result["delete_error"]
        action = t.store.get_actions()[0]
        assert json.loads(action.undo_json)["new_ytm"] == "PL_NEW_0"
        assert any(p.ytm_playlist_id == "PL_NEW_0" for p in t.store.get_playlists())
    assert t.source_client.deleted == []
    assert t.store.get_playlist(t.source) is not None


@pytest.mark.parametrize("operation", ["merge", "move", "delete_duplicate"])
@pytest.mark.parametrize("failure", ["timeout", "malformed", "truncated", "unresolved", "error_status"])
def test_uncertain_destination_never_authorizes_deletion(transfer, monkeypatch, operation, failure):
    t = transfer
    get = t.target_client.get_playlist
    if operation == "delete_duplicate":
        t.target_client._tracks["KEEP"] = list(t.tracks)

    def uncertain_read(playlist, limit=None):
        detail = get(playlist, limit)
        # Merges first read the keeper to capture undo and decide what to add. Fail the readback.
        if operation == "merge" and not t.target_client.added:
            return detail
        if failure == "timeout":
            raise TimeoutError("Playlist read timed out")
        if failure == "malformed":
            return {}
        if failure == "truncated":
            return {**detail, "trackCount": len(detail["tracks"]) + 1}
        if failure == "unresolved":
            return {"tracks": [*detail["tracks"], {"title": "Unavailable"}]}
        return {**detail, "status": "STATUS_FAILED"}

    monkeypatch.setattr(t.target_client, "get_playlist", uncertain_read)
    if operation == "move":
        result = _run(t, operation)
        assert not result["deleted"]
        assert "Refusing to delete" in result["delete_error"]
        assert t.store.get_actions()[0].kind == "move_identity"
    else:
        with pytest.raises(ValueError, match="Refusing to delete"):
            _run(t, operation)
    assert not t.source_client.deleted
    assert t.store.get_playlist(t.source) is not None


@pytest.mark.parametrize("operation", ["merge", "move", "delete_duplicate"])
def test_uncertain_source_does_not_fall_back_to_cached_contents(transfer, monkeypatch, operation):
    t = transfer
    t.target_client._tracks["KEEP"] = list(t.tracks)

    def unreadable(*args, **kwargs):
        raise TimeoutError("Source unavailable")

    monkeypatch.setattr(t.source_client, "get_playlist", unreadable)
    if operation == "move":
        result = _run(t, operation)
        assert not result["deleted"] and "source playlist" in result["delete_error"]
    else:
        with pytest.raises(ValueError, match="source playlist"):
            _run(t, operation)
    assert not t.source_client.deleted
    assert t.store.get_playlist(t.source) is not None


@pytest.mark.parametrize("operation", ["merge", "move"])
def test_unseen_remote_uploads_must_survive_too(transfer, operation):
    t = transfer
    t.source_client._tracks["SOURCE"].append(_track("new-upload", "New", "Artist"))
    if operation == "move":
        result = _run(t, operation)
        assert not result["deleted"] and "missing 1 required" in result["delete_error"]
    else:
        with pytest.raises(ValueError, match="missing 1 required"):
            _run(t, operation)
    assert not t.source_client.deleted


@pytest.mark.parametrize("selected", [["v1"], []])
def test_merge_honors_explicit_track_exclusions(transfer, selected):
    t = transfer
    result = executor.apply_result(t.store, t.clients, [t.keeper, t.source], selected, t.keeper, 10)
    assert result["deleted"] == ["Original"]
    assert [track["videoId"] for track in t.target_client._tracks["KEEP"]] == selected
    backup = json.loads(t.store.get_actions()[0].undo_json)["backups"][0]
    assert [track["video_id"] for track in json.loads(Path(backup).read_text())["tracks"]] == ["v1", "v2"]


def test_n_way_merge_rechecks_destination_before_each_deletion(transfer, monkeypatch):
    t = transfer
    other = t.store.upsert_playlist(t.source_identity, "OTHER", "Other", 2, "", 1)
    t.store.set_playlist_tracks(other, t.tids)
    t.source_client._tracks["OTHER"] = list(t.tracks)
    delete = t.source_client.delete_playlist

    def delete_then_change_destination(playlist):
        delete(playlist)
        t.target_client._tracks["KEEP"] = []

    monkeypatch.setattr(t.source_client, "delete_playlist", delete_then_change_destination)
    with pytest.raises(ValueError, match="Refusing to delete"):
        executor.apply_result(t.store, t.clients, [t.keeper, t.source, other],
                              ["v1", "v2"], t.keeper, 10)
    assert t.source_client.deleted == ["SOURCE"]
    assert t.store.get_playlist(t.source) is None
    assert t.store.get_playlist(other) is not None
    action = t.store.get_actions()[0]
    assert json.loads(action.params_json)["deleted"] == ["Original"]
    assert Path(json.loads(action.undo_json)["backups"][0]).is_file()


@pytest.mark.parametrize("operation", ["merge", "move"])
def test_lost_delete_response_preserves_backup_evidence(transfer, monkeypatch, operation):
    t = transfer

    def timeout_after_delete(playlist):
        t.source_client.deleted.append(playlist)
        raise TimeoutError("Delete response lost")

    monkeypatch.setattr(t.source_client, "delete_playlist", timeout_after_delete)
    if operation == "merge":
        with pytest.raises(TimeoutError):
            _run(t, operation)
        backup = json.loads(t.store.get_actions()[0].undo_json)["backups"][0]
    else:
        result = _run(t, operation)
        assert not result["deleted"] and "Delete response lost" in result["delete_error"]
        backup = json.loads(t.store.get_actions()[0].params_json)["source_backup"]
    assert Path(backup).is_file()
    assert len(json.loads(Path(backup).read_text())["tracks"]) == 2
    assert t.source_client.deleted == ["SOURCE"]  # non-idempotent deletes are never replayed


def test_failed_merge_route_preserves_draft_and_reports_partial_action(transfer, monkeypatch):
    from fastapi.testclient import TestClient
    from yt_playlist.web.app import create_app

    t = transfer
    monkeypatch.setattr(t.target_client, "add_playlist_items", lambda *a: {"status": "STATUS_FAILED"})
    client = TestClient(create_app(t.store, lambda: t.clients, now_fn=lambda: 10),
                        base_url="http://127.0.0.1")
    client.get(f"/merge?ids={t.keeper},{t.source}")
    response = client.post(f"/merge/apply?ids={t.keeper},{t.source}")

    assert response.status_code == 422
    assert "Refusing to delete" in response.text
    assert "hx-redirect" not in response.headers
    assert t.store.get_draft(f"{t.keeper},{t.source}") is not None
    action = next(a for a in t.store.get_actions() if a.kind == "apply_merge")
    params = json.loads(action.params_json)
    assert params["partial"] and params["skipped"] == 1
    assert "missing 1 required" in params["error"]
    assert t.store.get_playlist(t.source) is not None
    assert not t.source_client.deleted
    log = client.get("/actions").text
    assert "partial merge into" in log
    assert "missing 1 required" in log
    assert f'/undo/{action.id}' in log
