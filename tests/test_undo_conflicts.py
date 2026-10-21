import json

import pytest

from tests.conftest import FakeClient, _track
from tests.test_undo_order import OccurrenceClient
from yt_playlist.library import executor, playlist_undo
from yt_playlist.library.undo_conflicts import playlist_state
from yt_playlist.util.action_kinds import APPLY_MERGE, COPY_PLAYLIST


def _merge_action(store, before, after, client, *, evidence=True):
    iid = store.identities.upsert_identity("Main", "bridge", None, True)
    store.playlists.upsert_playlist(iid, "KEEP", "Kept", len(after), "", 1)
    entry = {"ytm": "KEEP", "identity": iid, "prev": before}
    if evidence:
        entry.update(post=after, post_state=playlist_state(client.get_playlist("KEEP", limit=None)))
    aid = store.actions.record_action(APPLY_MERGE, "{}", "{}", "executed",
                               json.dumps({"restored": [entry]}), 1)
    return iid, aid


@pytest.mark.parametrize("before,after,current,expected", [
    (["a", "b", "c"], ["a", "c"], ["a", "x", "c"], ["a", "x", "b", "c"]),
    (["a", "b", "c"], ["a", "c"], ["a"], ["a", "b"]),
    (["a", "b"], ["a", "b", "d"], ["a", "b", "d", "x"], ["a", "b", "x"]),
    (["a", "b"], ["a", "b", "d"], ["a", "d"], ["a"]),
    (["a", "b", "c"], ["a", "c"], ["x", "a", "c", "y"], ["x", "a", "b", "c", "y"]),
])
def test_inverse_preserves_independent_later_additions_and_removals(
        store, before, after, current, expected):
    client = OccurrenceClient(current)
    iid, aid = _merge_action(store, before, after, client)
    undo_action = playlist_undo.undo_action
    undo_action(store, aid, {iid: client}, 2)
    assert [t["videoId"] for t in client._tracks["KEEP"]] == expected
    assert store.actions.get_action(aid).status == "undone"


@pytest.mark.parametrize("before,after,current", [
    (["a", "b", "c"], ["a", "c"], ["c", "a"]),
    (["a", "b", "c"], ["a", "c"], ["a", "b", "c"]),
    (["a", "b", "a"], ["a", "a"], ["a", "x", "a"]),
])
def test_conflicting_edits_are_preserved_and_reported(store, before, after, current):
    client = OccurrenceClient(current)
    iid, aid = _merge_action(store, before, after, client)
    with pytest.raises(ValueError, match="Undo conflict"):
        playlist_undo.undo_action(store, aid, {iid: client}, 2)
    assert not client.added and not client.removed and not client.edited
    assert [t["videoId"] for t in client._tracks["KEEP"]] == current
    assert store.actions.get_action(aid).status != "undone"


def test_later_replacement_of_action_added_occurrence_is_not_removed(store):
    client = OccurrenceClient(["a", "d"])
    iid, aid = _merge_action(store, ["a"], ["a", "d"], client)
    client._tracks["KEEP"][1]["setVideoId"] = "replacement"
    with pytest.raises(ValueError, match="replaced"):
        playlist_undo.undo_action(store, aid, {iid: client}, 2)
    assert not client.removed


@pytest.mark.parametrize("change", ["add", "remove", "reorder", "rename", "description", "replace"])
@pytest.mark.parametrize("move", [False, True])
def test_edited_copy_destination_is_never_deleted(store, monkeypatch, tmp_path, change, move):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    iid = store.identities.upsert_identity("Main", "bridge", None, True)
    pid = store.playlists.upsert_playlist(iid, "SOURCE", "Source", 2, "", 1)
    tids = [store.tracks.upsert_track(v, v, "Artist", None, 200) for v in ("a", "b")]
    store.playlists.set_playlist_tracks(pid, tids)
    client = FakeClient(tracks={"SOURCE": [_track(v, v, "Artist", set_video_id=v) for v in ("a", "b")]})
    source_client = client
    if move:
        iid = store.identities.upsert_identity("Target", "bridge", None, False)
        client = FakeClient(catalog=source_client._catalog)
    get = client.get_playlist
    metadata = {"title": "Copy", "description": "Copied by TuneConsole"}
    monkeypatch.setattr(client, "get_playlist", lambda *a, **kw: {**get(*a, **kw), **metadata})
    if move:
        executor.copy_or_move_playlist(store, pid, iid, source_client, client, 1, delete_source=True)
    else:
        executor.copy_playlist(store, [pid], "Copy", client, 1)
    aid = store.actions.get_actions()[0].id
    created = json.loads(store.actions.get_action(aid).undo_json)["new_ytm"]
    if change == "add":
        client._tracks[created].append(_track("later", "Later", "Artist"))
    elif change == "remove":
        client._tracks[created].pop()
    elif change == "reorder":
        client._tracks[created].reverse()
    elif change == "replace":
        client._tracks[created][0] = {**client._tracks[created][0], "setVideoId": "replaced"}
    else:
        metadata["title" if change == "rename" else "description"] = "Edited later"
    with pytest.raises(ValueError, match="created playlist has changed"):
        playlist_undo.undo_action(store, aid, {1: source_client, iid: client}, 2)
    assert not client.deleted
    assert store.actions.get_action(aid).status != "undone"


@pytest.mark.parametrize("kind", [APPLY_MERGE, COPY_PLAYLIST])
def test_legacy_actions_without_post_action_evidence_require_manual_review(store, kind):
    client = OccurrenceClient(["a", "later"])
    iid, aid = _merge_action(store, ["a", "b"], ["a"], client, evidence=False)
    if kind == COPY_PLAYLIST:
        aid = store.actions.record_action(kind, "{}", "{}", "executed", json.dumps({
            "new_ytm": "KEEP", "target_identity": iid}), 1)
    with pytest.raises(ValueError, match="older.*snapshot"):
        playlist_undo.undo_action(store, aid, {iid: client}, 2)
    assert not client.deleted and not client.removed and not client.added
    assert store.actions.get_action(aid).status != "undone"


def test_forward_merge_records_actual_post_action_sequence(store):
    iid = store.identities.upsert_identity("Main", "bridge", None, True)
    a = store.playlists.upsert_playlist(iid, "KEEP", "A", 2, "", 1)
    b = store.playlists.upsert_playlist(iid, "OTHER", "B", 0, "", 1)
    client = OccurrenceClient(["a", "b"])
    client._tracks["OTHER"] = []
    executor.apply_result(store, {iid: client}, [a, b], ["a", "c"], "all", 2)
    entries = json.loads(store.actions.get_actions()[0].undo_json)["restored"]
    for entry in entries:
        assert entry["post"] == [t["videoId"] for t in client._tracks[entry["ytm"]]]
        assert entry["post_state"]["handles"]
