"""A failed merge Undo must retain the keeper's usable copy until sources are restored."""
import json
from pathlib import Path

import pytest

from tests.conftest import _track
from tests.test_undo_recovery import RecoveryClient, _journal
from yt_playlist.library import executor, playlist_undo, playlist_backups
from yt_playlist.util.action_kinds import APPLY_MERGE


@pytest.fixture
def merged(store, tmp_path, monkeypatch):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    client = RecoveryClient({"SOURCE": ["b"], "KEEP": ["a", "b"]})
    iid = store.identities.upsert_identity("Main", "bridge", None, True)
    source = store.playlists.upsert_playlist(iid, "SOURCE", "Source", 1, "", 1)
    store.playlists.upsert_playlist(iid, "KEEP", "Keeper", 2, "", 1)
    backup = playlist_backups.backup_playlist(store, source, 2, client)
    client.delete_playlist("SOURCE")
    store.playlists.remove_playlist(source)
    undo = {"backups": [backup], "restored": [
        {"ytm": "KEEP", "identity": iid, "prev": ["a"], "post": ["a", "b"]},
    ]}
    aid = store.actions.record_action(APPLY_MERGE, "{}", "{}", "executed", json.dumps(undo), 2)
    return client, iid, aid, undo


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("failure", ["backup_read", "account", "create", "restore"])
def test_merge_source_failures_preserve_keeper_and_resume_safely(store, merged, monkeypatch,
                                                               legacy, failure):
    client, iid, aid, undo = merged
    if legacy:
        steps = playlist_undo._steps(undo, aid)
        steps.sort(key=lambda step: step["kind"] != "keeper")
        undo["recovery"] = {"version": 1, "steps": steps}
        store.actions.update_action(aid, "undo_failed", 2, json.dumps(undo))
    clients = {iid: client}
    with monkeypatch.context() as patch:
        if failure == "backup_read":
            read = Path.read_text

            def unreadable(path, *args, **kwargs):
                if str(path) == undo["backups"][0]:
                    raise OSError("Backup unavailable")
                return read(path, *args, **kwargs)

            patch.setattr(Path, "read_text", unreadable)
        elif failure == "account":
            clients = {}
        elif failure == "create":
            patch.setattr(client, "reject_create_after", 0)
        else:
            patch.setattr(client, "reject_add", True)
        with pytest.raises(ValueError, match="Undo incomplete"):
            playlist_undo.undo_action(store, aid, clients, 3)
    assert [t["videoId"] for t in client._tracks["KEEP"]] == ["a", "b"]
    assert not client.removed
    journal = _journal(store, aid)
    assert journal["version"] == 2 and journal["steps"][0]["kind"] == "backup"
    source = journal["steps"][0]
    marker, created_id = source["marker"], source.get("created_id")
    playlist_undo.undo_action(store, aid, {iid: client}, 4)
    assert [t["videoId"] for t in client._tracks["KEEP"]] == ["a"]
    assert [t["videoId"] for t in client._tracks["PL_NEW_0"]] == ["b"]
    assert len(client.created) == 1
    source = _journal(store, aid)["steps"][0]
    assert source["marker"] == marker
    assert not created_id or source["created_id"] == created_id
    assert store.actions.get_action(aid).status == "undone"


def test_completed_source_is_rechecked_before_retrying_keeper_and_later_edits_survive(
    store, merged, monkeypatch,
):
    client, iid, aid, _ = merged
    remove = client.remove_playlist_items
    monkeypatch.setattr(client, "remove_playlist_items", lambda *args: "STATUS_FAILED")
    with pytest.raises(ValueError, match="Undo incomplete"):
        playlist_undo.undo_action(store, aid, {iid: client}, 3)
    assert _journal(store, aid)["steps"][0]["phase"] == "complete"
    client._tracks["PL_NEW_0"] = []
    monkeypatch.setattr(client, "remove_playlist_items", remove)
    with pytest.raises(ValueError, match="restored source is missing or has changed"):
        playlist_undo.undo_action(store, aid, {iid: client}, 4)
    assert [t["videoId"] for t in client._tracks["KEEP"]] == ["a", "b"]
    client._tracks["PL_NEW_0"] = [_track("b", "b", "Artist"), _track("later", "Later", "Artist")]
    playlist_undo.undo_action(store, aid, {iid: client}, 5)
    assert len(client.created) == 1
    assert [t["videoId"] for t in client._tracks["PL_NEW_0"]] == ["b", "later"]
    assert [t["videoId"] for t in client._tracks["KEEP"]] == ["a"]


def test_legacy_partial_journal_retains_created_id_and_conflict_baselines(store, merged):
    client, iid, aid, _ = merged
    client.reject_add = True
    with pytest.raises(ValueError, match="Undo incomplete"):
        playlist_undo.undo_action(store, aid, {iid: client}, 3)
    undo = json.loads(store.actions.get_action(aid).undo_json)
    journal = undo["recovery"]
    journal["version"] = 1
    journal["steps"].sort(key=lambda step: step["kind"] != "keeper")
    source = dict(journal["steps"][1])
    assert source["created_id"] == "PL_NEW_0" and source["phase"] == "restoring"
    store.actions.update_action(aid, "undo_failed", 3, json.dumps(undo))
    # Failure before another restore write lets us inspect the migrated state exactly.
    with pytest.raises(ValueError, match="Account .* unavailable"):
        playlist_undo.undo_action(store, aid, {}, 4)
    migrated = _journal(store, aid)
    assert migrated["version"] == 2 and migrated["steps"][0] == source
    client.reject_add = False
    playlist_undo.undo_action(store, aid, {iid: client}, 5)
    assert len(client.created) == 1
    assert [t["videoId"] for t in client._tracks["PL_NEW_0"]] == ["b"]
    assert [t["videoId"] for t in client._tracks["KEEP"]] == ["a"]
