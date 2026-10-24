import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from ytmusicapi.exceptions import YTMusicServerError

from tests.conftest import FakeClient, _track
from yt_playlist.core.store import Store
from yt_playlist.library import executor, playlist_undo, playlist_backups
from yt_playlist.library.undo_conflicts import playlist_state
from yt_playlist.util.action_kinds import APPLY_MERGE, COPY_PLAYLIST, UNDO


class RecoveryClient(FakeClient):
    def __init__(self, tracks):
        super().__init__(tracks={pid: [_track(v, v, "Artist", set_video_id=f"{pid}-{i}")
                                      for i, v in enumerate(vids)] for pid, vids in tracks.items()})
        self.metadata = {pid: {"title": pid, "description": "Original"} for pid in tracks}
        self.reject_add = False
        self.reject_delete = False
        self.reject_create_after = None
        self.lose_create = False
        self.lose_add = False
        self.lose_delete = False
        self.hide_created = False
        self.serial = 100

    def get_playlist(self, playlistId, limit=None):
        if playlistId not in self._tracks:
            raise YTMusicServerError("Server returned HTTP 404: Not Found.")
        return {**super().get_playlist(playlistId, limit), **self.metadata.get(playlistId, {})}

    def get_library_playlists(self, limit=None):
        return [{"playlistId": p} for p in self._tracks if p not in self.deleted
                and (not self.hide_created or not p.startswith("PL_NEW"))]

    def create_playlist(self, title, description):
        if self.reject_create_after is not None and len(self.created) >= self.reject_create_after:
            raise YTMusicServerError("Server returned HTTP 401: Unauthorized.")
        pid = super().create_playlist(title, description)
        self.metadata[pid] = {"title": title, "description": description}
        if self.lose_create:
            raise TimeoutError("Creation reply lost")
        return pid

    def add_playlist_items(self, playlistId, videoIds, *, duplicates=False):
        self.added.append((playlistId, list(videoIds)))
        if self.reject_add:
            return {"status": "STATUS_FAILED"}
        for v in videoIds:
            self._tracks[playlistId].append(_track(v, v, "Artist", set_video_id=f"new-{self.serial}"))
            self.serial += 1
        if self.lose_add:
            raise TimeoutError("Addition reply lost")
        return {"status": "STATUS_SUCCEEDED"}

    def delete_playlist(self, playlistId):
        if self.reject_delete:
            return "STATUS_FAILED"
        super().delete_playlist(playlistId)
        if self.lose_delete:
            raise TimeoutError("Deletion reply lost")
        return "STATUS_SUCCEEDED"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))


def _deleted(store, client):
    iid = store.identities.upsert_identity("Main", "bridge", None, True)
    pid = store.playlists.upsert_playlist(iid, "SOURCE", "Source", 2, "", 1)
    executor.delete_playlist(store, pid, client, 2)
    return iid, store.actions.get_actions()[0].id


def _journal(store, aid):
    return json.loads(store.actions.get_action(aid).undo_json)["recovery"]


def _copy(store, client):
    iid = store.identities.upsert_identity("Main", "bridge", None, True)
    store.playlists.upsert_playlist(iid, "COPY", "Copy", 2, "", 1)
    aid = store.actions.record_action(COPY_PLAYLIST, "{}", "{}", "executed", json.dumps({
        "new_ytm": "COPY", "target_identity": iid,
        "post": playlist_state(client.get_playlist("COPY", limit=None)),
    }), 1)
    return iid, aid


def test_rejected_additions_resume_created_playlist_after_database_reopen(tmp_path):
    store = Store(tmp_path / "recovery.db")
    store.init_schema()
    client = RecoveryClient({"SOURCE": ["a", "b"]})
    iid, aid = _deleted(store, client)
    client.reject_add = True
    with pytest.raises(ValueError, match="Undo incomplete"):
        playlist_undo.undo_action(store, aid, {iid: client}, 3)
    step, = _journal(store, aid)["steps"]
    assert step["created_id"] == "PL_NEW_0"
    assert store.actions.get_action(aid).status == "undo_failed"
    assert not any(a.kind == UNDO for a in store.actions.get_actions())
    store.conn.close()
    reopened = Store(tmp_path / "recovery.db")
    reopened.init_schema()
    client.reject_add = False
    playlist_undo.undo_action(reopened, aid, {iid: client}, 4)
    assert len(client.created) == 1
    assert [t["videoId"] for t in client._tracks["PL_NEW_0"]] == ["a", "b"]
    assert reopened.actions.get_action(aid).status == "undone"
    assert sum(a.kind == UNDO for a in reopened.actions.get_actions()) == 1
    reopened.conn.close()


def test_failure_on_later_backup_does_not_recreate_completed_first_backup(store):
    client = RecoveryClient({"ONE": ["a"], "TWO": ["b"]})
    iid = store.identities.upsert_identity("Main", "bridge", None, True)
    backups = []
    for remote in ("ONE", "TWO"):
        pid = store.playlists.upsert_playlist(iid, remote, remote, 1, "", 1)
        backups.append(playlist_backups.backup_playlist(store, pid, 2, client))
        client.delete_playlist(remote)
        store.playlists.remove_playlist(pid)
    aid = store.actions.record_action(APPLY_MERGE, "{}", "{}", "executed",
                               json.dumps({"backups": backups}), 2)
    client.reject_create_after = 1
    with pytest.raises(ValueError, match="1/2 steps complete"):
        playlist_undo.undo_action(store, aid, {iid: client}, 3)
    first, second = _journal(store, aid)["steps"]
    assert first["phase"] == "complete" and second["phase"] == "pending"
    client.reject_create_after = None
    playlist_undo.undo_action(store, aid, {iid: client}, 4)
    assert len(client.created) == 2
    assert [t["videoId"] for t in client._tracks["PL_NEW_0"]] == ["a"]
    assert [t["videoId"] for t in client._tracks["PL_NEW_1"]] == ["b"]


@pytest.mark.parametrize("lag", [False, True])
def test_lost_creation_reply_is_reconciled_by_durable_marker_without_recreating(store, lag):
    client = RecoveryClient({"SOURCE": ["a", "b"]})
    iid, aid = _deleted(store, client)
    client.lose_create = True
    client.hide_created = lag
    if lag:
        for now in (3, 4):
            with pytest.raises(ValueError, match="creation response was lost"):
                playlist_undo.undo_action(store, aid, {iid: client}, now)
            assert len(client.created) == 1
        client.hide_created = False
    playlist_undo.undo_action(store, aid, {iid: client}, 5)
    assert len(client.created) == 1
    assert store.actions.get_action(aid).status == "undone"


def test_lost_addition_reply_is_verified_instead_of_duplicating_tracks(store):
    client = RecoveryClient({"SOURCE": ["a", "b"]})
    iid, aid = _deleted(store, client)
    client.lose_add = True
    playlist_undo.undo_action(store, aid, {iid: client}, 3)
    assert client.added == [("PL_NEW_0", ["a", "b"])]
    assert store.actions.get_action(aid).status == "undone"


@pytest.mark.parametrize("lost_reply", [False, True])
def test_copy_deletion_requires_verified_absence_and_resumes_safely(store, lost_reply):
    client = RecoveryClient({"COPY": ["a", "b"]})
    iid, aid = _copy(store, client)
    client.reject_delete = not lost_reply
    client.lose_delete = lost_reply
    if not lost_reply:
        with pytest.raises(ValueError, match="not confirmed deletion"):
            playlist_undo.undo_action(store, aid, {iid: client}, 2)
        assert store.actions.get_action(aid).status == "undo_failed"
        assert store.playlists.get_playlists()
        client.reject_delete = False
    playlist_undo.undo_action(store, aid, {iid: client}, 3)
    assert store.actions.get_action(aid).status == "undone"
    assert not store.playlists.get_playlists()
    assert client.deleted == ["COPY"]


def test_original_surviving_failed_forward_delete_is_reused_without_duplicate(store, monkeypatch):
    client = RecoveryClient({"SOURCE": ["a"], "KEEP": ["a"]})
    iid = store.identities.upsert_identity("Main", "bridge", None, True)
    src = store.playlists.upsert_playlist(iid, "SOURCE", "Source", 1, "", 1)
    keep = store.playlists.upsert_playlist(iid, "KEEP", "Keeper", 1, "", 1)
    delete = client.delete_playlist

    def failed_delete(pid):
        raise TimeoutError("Forward deletion reply lost")

    monkeypatch.setattr(client, "delete_playlist", failed_delete)
    with pytest.raises(TimeoutError):
        executor.apply_result(store, {iid: client}, [src, keep], ["a"], keep, 2)
    aid = store.actions.get_actions()[0].id
    # A later edit to the surviving original is independent of this incomplete forward delete.
    client._tracks["SOURCE"].append(_track("later", "Later", "Artist", set_video_id="later"))
    monkeypatch.setattr(client, "delete_playlist", delete)
    playlist_undo.undo_action(store, aid, {iid: client}, 3)
    assert not client.created
    assert [t["videoId"] for t in client._tracks["SOURCE"]] == ["a", "later"]
    backup = next(s for s in _journal(store, aid)["steps"] if s["kind"] == "backup")
    assert backup["original_survived"]


def test_connection_failure_cannot_be_mistaken_for_missing_original(store, monkeypatch):
    client = RecoveryClient({"SOURCE": ["a", "b"]})
    iid, aid = _deleted(store, client)
    get = client.get_playlist
    monkeypatch.setattr(client, "get_playlist", lambda *a, **kw: (_ for _ in ()).throw(TimeoutError()))
    with pytest.raises(ValueError, match="whether playlist SOURCE exists"):
        playlist_undo.undo_action(store, aid, {iid: client}, 3)
    assert not client.created
    monkeypatch.setattr(client, "get_playlist", get)
    playlist_undo.undo_action(store, aid, {iid: client}, 4)
    assert len(client.created) == 1


def test_move_with_lost_forward_delete_reply_retains_and_restores_its_backup(store):
    source = RecoveryClient({"SOURCE": ["a", "b"]})
    destination = RecoveryClient({})
    iid = store.identities.upsert_identity("Main", "bridge", None, True)
    other = store.identities.upsert_identity("Other", "bridge", None, False)
    pid = store.playlists.upsert_playlist(iid, "SOURCE", "Source", 2, "", 1)
    store.playlists.set_playlist_tracks(pid, [store.tracks.upsert_track(v, v, "Artist", None, 200) for v in ("a", "b")])
    source.lose_delete = True
    result = executor.copy_or_move_playlist(store, pid, other, source, destination, 2, delete_source=True)
    assert not result["deleted"] and result["delete_error"]
    aid = store.actions.get_actions()[0].id
    assert json.loads(store.actions.get_action(aid).undo_json)["backup"]
    playlist_undo.undo_action(store, aid, {iid: source, other: destination}, 3)
    assert len(source.created) == 1
    assert [t["videoId"] for t in source._tracks["PL_NEW_0"]] == ["a", "b"]
    assert destination.deleted == ["PL_NEW_0"]
    assert store.actions.get_action(aid).status == "undone"


def test_created_id_survives_indexing_lag_before_first_track_write(store, monkeypatch):
    client = RecoveryClient({"SOURCE": ["a", "b"]})
    iid, aid = _deleted(store, client)
    get = client.get_playlist
    lagging = True

    def read(pid, limit=None):
        if pid.startswith("PL_NEW") and lagging:
            raise YTMusicServerError("Server returned HTTP 404: Not Found.")
        return get(pid, limit)

    monkeypatch.setattr(client, "get_playlist", read)
    with pytest.raises(ValueError, match="not available yet"):
        playlist_undo.undo_action(store, aid, {iid: client}, 3)
    assert len(client.created) == 1 and not client.added
    assert _journal(store, aid)["steps"][0]["created_id"] == "PL_NEW_0"
    lagging = False
    playlist_undo.undo_action(store, aid, {iid: client}, 4)
    assert len(client.created) == 1
    assert store.actions.get_action(aid).status == "undone"


def test_incomplete_keeper_recovery_resumes_position_changes_without_duplicate_additions(store):
    from tests.test_undo_order import OccurrenceClient, _action
    client = OccurrenceClient(["a", "c"])
    iid, _, aid = _action(store, ["a", "b", "c"], ["a", "c"])
    client.reject_moves = True
    with pytest.raises(ValueError, match="position change"):
        playlist_undo.undo_action(store, aid, {iid: client}, 3)
    assert [t["videoId"] for t in client._tracks["KEEP"]] == ["a", "c", "b"]
    assert len(client.added) == 1
    client.reject_moves = False
    playlist_undo.undo_action(store, aid, {iid: client}, 4)
    assert [t["videoId"] for t in client._tracks["KEEP"]] == ["a", "b", "c"]
    assert len(client.added) == 1


def test_simultaneous_undo_requests_have_one_owner_across_database_connections(tmp_path, monkeypatch):
    database = tmp_path / "parallel.db"
    store = Store(database)
    store.init_schema()
    client = RecoveryClient({"SOURCE": ["a", "b"]})
    iid, aid = _deleted(store, client)
    other = Store(database)
    other.init_schema()
    started, release = threading.Event(), threading.Event()
    create = client.create_playlist

    def blocked_create(*args):
        started.set()
        assert release.wait(5)
        return create(*args)

    monkeypatch.setattr(client, "create_playlist", blocked_create)
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(playlist_undo.undo_action, store, aid, {iid: client}, 3)
        try:
            assert started.wait(5)
            with pytest.raises(ValueError, match="already running"):
                playlist_undo.undo_action(other, aid, {iid: client}, 4)
        finally:
            release.set()
        running.result(timeout=5)
    assert len(client.created) == 1
    assert sum(a.kind == UNDO for a in store.actions.get_actions()) == 1
    other.conn.close()
    store.conn.close()


def test_abandoned_claim_resumes_after_crash_without_recreating_or_overwriting_edits(store, monkeypatch):
    client = RecoveryClient({"SOURCE": ["a", "b"]})
    iid, aid = _deleted(store, client)
    refresh = playlist_undo._refresh

    def crash(*args, **kwargs):
        raise SystemExit("process stopped")

    monkeypatch.setattr(playlist_undo, "_refresh", crash)
    with pytest.raises(SystemExit):
        playlist_undo.undo_action(store, aid, {iid: client}, 3)
    assert store.actions.get_action(aid).status == "undoing"
    assert _journal(store, aid)["steps"][0]["phase"] == "remote_complete"
    client._tracks["PL_NEW_0"].append(_track("later", "Later", "Artist"))
    monkeypatch.setattr(playlist_undo, "_refresh", refresh)
    playlist_undo.undo_action(store, aid, {iid: client}, 4)
    assert len(client.created) == 1
    assert [t["videoId"] for t in client._tracks["PL_NEW_0"]] == ["a", "b", "later"]
    assert store.actions.get_action(aid).status == "undone"


def test_partial_recovery_remains_visible_and_retryable_after_action_log_reload(store):
    from fastapi.testclient import TestClient
    from yt_playlist.web.app import create_app
    client = RecoveryClient({"SOURCE": ["a", "b"]})
    iid, aid = _deleted(store, client)
    client.reject_add = True
    web = TestClient(create_app(store, lambda: {iid: client}), base_url="http://127.0.0.1")
    response = web.post(f"/undo/{aid}")
    assert response.status_code == 200
    assert "Undo incomplete" in response.text
    for _ in range(2):
        log = web.get("/actions").text
        assert "recovery incomplete" in log and "Resume Undo" in log
        assert "PL_NEW_0" in log and "Progress is saved" in log
    client.reject_add = False
    response = web.post(f"/undo/{aid}")
    assert response.status_code == 200
    assert store.actions.get_action(aid).status == "undone"
    assert len(client.created) == 1


def test_crash_after_creation_before_add_can_resume_from_verified_unchanged_baseline(store, monkeypatch):
    client = RecoveryClient({"SOURCE": ["a", "b"]})
    iid, aid = _deleted(store, client)
    add = client.add_playlist_items

    def crash(*args, **kwargs):
        raise SystemExit("stopped before add")

    monkeypatch.setattr(client, "add_playlist_items", crash)
    with pytest.raises(SystemExit):
        playlist_undo.undo_action(store, aid, {iid: client}, 3)
    assert _journal(store, aid)["steps"][0]["created_id"] == "PL_NEW_0"
    monkeypatch.setattr(client, "add_playlist_items", add)
    playlist_undo.undo_action(store, aid, {iid: client}, 4)
    assert len(client.created) == 1
    assert store.actions.get_action(aid).status == "undone"


def test_failed_removal_cannot_mark_keeper_restored(store, monkeypatch):
    from tests.test_undo_order import OccurrenceClient, _action
    client = OccurrenceClient(["a", "extra"])
    iid, _, aid = _action(store, ["a"], ["a", "extra"])
    remove = client.remove_playlist_items
    monkeypatch.setattr(client, "remove_playlist_items", lambda *args: "STATUS_FAILED")
    with pytest.raises(ValueError, match="Undo incomplete"):
        playlist_undo.undo_action(store, aid, {iid: client}, 3)
    assert store.actions.get_action(aid).status == "undo_failed"
    monkeypatch.setattr(client, "remove_playlist_items", remove)
    playlist_undo.undo_action(store, aid, {iid: client}, 4)
    assert [t["videoId"] for t in client._tracks["KEEP"]] == ["a"]


def test_completion_log_and_original_status_are_atomic_and_retryable(store):
    client = RecoveryClient({"SOURCE": ["a", "b"]})
    iid, aid = _deleted(store, client)
    store.conn.execute("CREATE TRIGGER fail_undo_log BEFORE INSERT ON actions "
                       "WHEN NEW.kind='undo' BEGIN SELECT RAISE(ABORT,'log unavailable'); END")
    with pytest.raises(ValueError, match="log unavailable"):
        playlist_undo.undo_action(store, aid, {iid: client}, 3)
    assert store.actions.get_action(aid).status == "undo_failed"
    assert not any(a.kind == UNDO for a in store.actions.get_actions())
    store.conn.execute("DROP TRIGGER fail_undo_log")
    playlist_undo.undo_action(store, aid, {iid: client}, 4)
    assert store.actions.get_action(aid).status == "undone"
    assert sum(a.kind == UNDO for a in store.actions.get_actions()) == 1
    assert len(client.created) == 1


def test_missing_recovery_evidence_does_not_report_success(store):
    aid = store.actions.record_action(COPY_PLAYLIST, "{}", "{}", "executed", "{}", 1)
    with pytest.raises(ValueError, match="no recovery snapshot"):
        playlist_undo.undo_action(store, aid, {}, 2)
    assert store.actions.get_action(aid).status == "undo_failed"
