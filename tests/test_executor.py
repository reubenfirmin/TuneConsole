import json
from pathlib import Path
import pytest
from yt_playlist.library.executor import (
    backup_playlist, deserialize_plan, store_plan, execute_planned, undo_action,
    MergePlan, Resolution)
from tests.conftest import FakeClient, _track

def _sample_plan():
    return MergePlan(1, 2,
        [Resolution("a|x", "v1", "v1", "reuse"), Resolution("b|x", None, "found", "search")],
        [Resolution("c|x", None, None, "unresolved")])

def test_serialize_deserialize_roundtrip(store):
    plan = _sample_plan()
    aid = store_plan(store, plan, "delete", "SRCYT", now=5.0)
    action = store.get_action(aid)
    assert action.kind == "plan" and action.status == "planned"
    pe = deserialize_plan(action)
    assert pe.mode == "delete" and pe.source_ytm_playlist_id == "SRCYT"
    assert pe.plan.source_playlist_id == 1 and pe.plan.target_playlist_id == 2
    assert [r.identity_key for r in pe.plan.additions] == ["a|x", "b|x"]
    assert [r.method for r in pe.plan.additions] == ["reuse", "search"]
    assert [r.identity_key for r in pe.plan.unresolved] == ["c|x"]
    assert pe.plan.additions[1].target_video_id == "found"


def test_backup_filename_sanitizes_remote_playlist_id(store, monkeypatch, tmp_path):
    # ytm_playlist_id comes from the YouTube API; a "../" must not escape backups dir.
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    iid = store.upsert_identity("main", "cred", None, True)
    pid = store.upsert_playlist(iid, "../../../../etc/pwned", "Evil", 0, "h", 1.0)
    path = backup_playlist(store, pid, 123.0)
    from yt_playlist.core import paths
    backups = paths.backups_dir().resolve()
    assert Path(path).resolve().parent == backups        # stayed inside backups dir
    assert "/" not in Path(path).name.replace(".json", "").split("_")[0]


def test_execute_planned_delete_removes_identical_copy(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    from yt_playlist.library.executor import store_plan, execute_planned, MergePlan
    from tests.conftest import FakeClient, _track
    iid = store.upsert_identity("main", "cred", None, True)
    t = store.upsert_track("v1", "Song", "Artist", None, 200)
    keep = store.upsert_playlist(iid, "KEEP", "Keep", 1, "h", 1.0); store.set_playlist_tracks(keep, [t])
    dele = store.upsert_playlist(iid, "DEL", "Dupe", 1, "h", 1.0); store.set_playlist_tracks(dele, [t])
    # remote KEEP already contains the track, so deletion is allowed
    client = FakeClient(tracks={"KEEP": [_track("v1", "Song", "Artist")]})
    plan = MergePlan(dele, keep, [], [])
    aid = store_plan(store, plan, "delete", "DEL", now=1.0)
    execute_planned(store, aid, {iid: client}, now=2.0)
    assert client.deleted == ["DEL"]                       # the redundant copy was deleted
    assert store.get_action(aid).status == "executed"

def test_execute_planned_delete_refuses_if_not_subset(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    from yt_playlist.library.executor import store_plan, execute_planned, MergePlan
    from tests.conftest import FakeClient, _track
    import pytest
    iid = store.upsert_identity("main", "cred", None, True)
    t1 = store.upsert_track("v1", "Song", "Artist", None, 200)
    t2 = store.upsert_track("v2", "Other", "Artist", None, 200)
    keep = store.upsert_playlist(iid, "KEEP", "Keep", 1, "h", 1.0); store.set_playlist_tracks(keep, [t1])
    dele = store.upsert_playlist(iid, "DEL", "Dupe", 2, "h", 1.0); store.set_playlist_tracks(dele, [t1, t2])
    # remote: DEL has both tracks, KEEP only one -> deleting DEL would lose "Other"
    client = FakeClient(tracks={"KEEP": [_track("v1", "Song", "Artist")],
                                "DEL": [_track("v1", "Song", "Artist"), _track("v2", "Other", "Artist")]})
    plan = MergePlan(dele, keep, [], [])
    aid = store_plan(store, plan, "delete", "DEL", now=1.0)
    with pytest.raises(ValueError, match="refusing to delete"):
        execute_planned(store, aid, {iid: client}, now=2.0)
    assert client.deleted == []                            # nothing deleted


def test_delete_prunes_row_and_undo_recreates_via_backup(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    from yt_playlist.library.executor import store_plan, execute_planned, undo_action, MergePlan
    from tests.conftest import FakeClient, _track
    iid = store.upsert_identity("main", "cred", None, True)
    t = store.upsert_track("v1", "Song", "Artist", None, 200)
    keep = store.upsert_playlist(iid, "KEEP", "Keep", 1, "h", 1.0); store.set_playlist_tracks(keep, [t])
    dele = store.upsert_playlist(iid, "DEL", "Dupe", 1, "h", 1.0); store.set_playlist_tracks(dele, [t])
    client = FakeClient(tracks={"KEEP": [_track("v1", "Song", "Artist")]}, catalog={"v1": _track("v1", "Song", "Artist")})
    aid = store_plan(store, MergePlan(dele, keep, [], []), "delete", "DEL", now=1.0)
    execute_planned(store, aid, {iid: client}, now=2.0)
    assert store.get_playlist(dele) is None                 # pruned from the dashboard
    undo_action(store, aid, {iid: client}, now=3.0)         # works despite the pruned row
    assert client.created                                   # recreated from backup (identity from backup)
    assert store.get_action(aid).status == "undone"


def test_apply_result_reconciles_and_deletes(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    from yt_playlist.library.executor import apply_result
    from tests.conftest import FakeClient, _track
    iid = store.upsert_identity("main", "cred", None, True)
    # A has v1,v2 ; B has v1,v3. Result = v1 + v3 (drop v2, add v3 from B). Keep A, delete B.
    a = store.upsert_playlist(iid, "PLA", "A", 2, "h", 1.0)
    b = store.upsert_playlist(iid, "PLB", "B", 2, "h", 1.0)
    client = FakeClient(tracks={"PLA": [_track("v1", "One", "X"), _track("v2", "Two", "X")],
                                "PLB": [_track("v1", "One", "X"), _track("v3", "Three", "X")]})
    s = apply_result(store, {iid: client}, [a, b], ["v1", "v3"], a, now=1.0)
    assert s["deleted"] == ["B"] and client.deleted == ["PLB"]
    # A reconciled to {v1, v3}: v3 added, v2 removed
    assert client.added == [("PLA", ["v3"])]
    assert ("PLA", [_track("v2", "Two", "X")]) == client.removed[0][0:1] + (client.removed[0][1],) \
        or any(pl == "PLA" and [t.get("videoId") for t in items] == ["v2"] for pl, items in client.removed)
    assert store.get_playlist(b) is None

def test_apply_result_update_both(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    from yt_playlist.library.executor import apply_result
    from tests.conftest import FakeClient, _track
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PLA", "A", 1, "h", 1.0)
    b = store.upsert_playlist(iid, "PLB", "B", 1, "h", 1.0)
    client = FakeClient(tracks={"PLA": [_track("v1", "One", "X")], "PLB": [_track("v2", "Two", "X")]})
    s = apply_result(store, {iid: client}, [a, b], ["v1", "v2"], "all", now=1.0)
    assert s["deleted"] == [] and client.deleted == []
    assert store.get_playlist(a) is not None and store.get_playlist(b) is not None


def test_resolve_in_target_rejects_low_confidence_match():
    # A merely-similar title with a very different duration must stay UNRESOLVED (so a move won't
    # delete the source for a wrong substitute); a duration-matching candidate resolves.
    from yt_playlist.library.executor import _resolve_in_target

    class SearchClient(FakeClient):
        def __init__(self, results):
            super().__init__()
            self._results = results

        def search(self, query, filter="songs"):
            return self._results

    # wrong cut: title close-ish but duration off by minutes, score < 0.95 -> unresolved
    wrong = SearchClient([{"title": "Time Zero (Extended Club Mix)", "artists": [{"name": "Artist"}],
                           "videoId": "WRONG", "duration_seconds": 600}])
    r = _resolve_in_target(wrong, "k", "Time Zero", "Artist", None, 200, 0.85)
    assert r.method == "unresolved" and r.target_video_id is None

    # right cut: duration within 3s -> resolved even if title isn't identical
    right = SearchClient([{"title": "Time Zero", "artists": [{"name": "Artist"}],
                           "videoId": "RIGHT", "duration_seconds": 201}])
    r = _resolve_in_target(right, "k", "Time Zero", "Artist", None, 200, 0.85)
    assert r.method == "search" and r.target_video_id == "RIGHT"


def test_apply_result_partial_failure_records_undoable_action(store, monkeypatch, tmp_path):
    # If a mutation throws mid-merge (here: a dropper delete fails), apply_result must still record an
    # undoable APPLY_MERGE with the kept playlist's prior contents — not exit leaving no undo trail.
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    from yt_playlist.library.executor import apply_result
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PLA", "A", 2, "h", 1.0)
    b = store.upsert_playlist(iid, "PLB", "B", 2, "h", 1.0)

    class FailDelete(FakeClient):
        def delete_playlist(self, playlistId):
            raise RuntimeError("youtube 500")

    client = FailDelete(tracks={"PLA": [_track("v1", "One", "X"), _track("v2", "Two", "X")],
                                "PLB": [_track("v1", "One", "X"), _track("v3", "Three", "X")]})
    with pytest.raises(RuntimeError):
        apply_result(store, {iid: client}, [a, b], ["v1", "v3"], a, now=1.0)   # keep A, delete B (fails)
    actions = [x for x in store.get_actions() if x.kind == "apply_merge"]
    assert actions, "a partial merge must still record an undoable action"
    undo = json.loads(actions[0].undo_json)
    prev = next(e["prev"] for e in undo["restored"] if e["ytm"] == "PLA")
    assert set(prev) == {"v1", "v2"}            # A's pre-merge contents captured for undo
    assert actions[0].status == "executed"      # undoable


def test_undo_apply_merge_restores(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    from yt_playlist.library.executor import apply_result, undo_action
    from tests.conftest import FakeClient, _track
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PLA", "A", 2, "h", 1.0)
    b = store.upsert_playlist(iid, "PLB", "B", 2, "h", 1.0)
    client = FakeClient(tracks={"PLA": [_track("v1", "One", "X"), _track("v2", "Two", "X")],
                                "PLB": [_track("v1", "One", "X"), _track("v3", "Three", "X")]},
                        catalog={v: _track(v, v, "X") for v in ("v1", "v2", "v3")})
    apply_result(store, {iid: client}, [a, b], ["v1", "v3"], a, now=1.0)   # keep A=v1,v3 ; delete B
    assert client.deleted == ["PLB"]
    aid = store.get_actions()[0].id
    assert store.get_action(aid).kind == "apply_merge"
    client.created = []  # reset to observe undo
    undo_action(store, aid, {iid: client}, now=2.0)
    assert client.created                                  # B recreated from backup
    assert store.get_action(aid).status == "undone"

def test_undo_delete_empty_recreates(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    from yt_playlist.library.executor import delete_empty_playlist, undo_action
    from tests.conftest import FakeClient
    iid = store.upsert_identity("main", "cred", None, True)
    p = store.upsert_playlist(iid, "PLe", "empty", 0, "h", 1.0)
    client = FakeClient()
    delete_empty_playlist(store, p, client, now=1.0)
    assert client.deleted == ["PLe"]
    aid = store.get_actions()[0].id
    undo_action(store, aid, {iid: client}, now=2.0)
    assert client.created and store.get_action(aid).status == "undone"
