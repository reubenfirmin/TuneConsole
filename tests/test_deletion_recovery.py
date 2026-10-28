"""Forward deletion keeps a discoverable recovery journal across uncertain writes and restarts."""
import json
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_undo_recovery import RecoveryClient
from yt_playlist.core.store import Store
from yt_playlist.library import executor, playlist_backups, playlist_deletion, playlist_undo, playlist_generated
from yt_playlist.web.app import create_app


@pytest.fixture
def deletion(tmp_path, monkeypatch):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    database = tmp_path / "deletion.db"
    store = Store(database)
    store.init_schema()
    identity = store.identities.upsert_identity("Main", "bridge", None, True)
    source = store.playlists.upsert_playlist(identity, "SOURCE", "Source", 2, "", 1)
    keeper = store.playlists.upsert_playlist(identity, "KEEP", "Keeper", 2, "", 1)
    client = RecoveryClient({"SOURCE": ["a", "b"], "KEEP": ["a", "b"]})
    yield store, database, identity, source, keeper, client
    store.conn.close()


def _reopen(database):
    store = Store(database)
    store.init_schema()
    return store


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory durability")
def test_first_backup_syncs_directory_ancestors_and_file_before_intent_and_delete(deletion, monkeypatch, tmp_path):
    store, _, _, source, _, client = deletion
    home = tmp_path / 'new-parent' / 'data'
    backups = home / 'backups'
    monkeypatch.setenv('YT_PLAYLIST_HOME', str(home))
    synced = set()
    fsync = playlist_backups.os.fsync

    def observe(fd):
        info = os.fstat(fd)
        fsync(fd)
        if stat.S_ISDIR(info.st_mode):
            for directory in (home.parent, home, backups):
                if directory.exists() and os.path.samestat(info, directory.stat()):
                    synced.add(directory)
        else:
            synced.add('file')

    monkeypatch.setattr(playlist_backups.os, 'fsync', observe)
    record_action = store.actions.record_action

    def record(*args, **kwargs):
        assert synced == {home.parent, home, backups, 'file'}
        return record_action(*args, **kwargs)

    monkeypatch.setattr(store.actions, 'record_action', record)
    delete = client.delete_playlist

    def delete_after_publication(playlist_id):
        assert synced == {home.parent, home, backups, 'file'}
        return delete(playlist_id)

    client.delete_playlist = delete_after_publication
    executor.delete_playlist(store, source, client, 3)
    assert client.deleted == ['SOURCE']


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory durability")
@pytest.mark.parametrize('ancestor', ['data', 'parent'])
def test_parent_sync_failure_prevents_delete_and_retry_resyncs_existing_directory(
        deletion, monkeypatch, tmp_path, ancestor):
    store, _, _, source, _, client = deletion
    home = tmp_path / 'new-parent' / 'data'
    backups = home / 'backups'
    target = home if ancestor == 'data' else home.parent
    monkeypatch.setenv('YT_PLAYLIST_HOME', str(home))
    fsync = playlist_backups.os.fsync
    attempts = []

    def fail_once(fd):
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode) and os.path.samestat(info, target.stat()):
            attempts.append(target)
            if len(attempts) == 1:
                raise OSError('parent sync failed')
        return fsync(fd)

    monkeypatch.setattr(playlist_backups.os, 'fsync', fail_once)
    with pytest.raises(OSError, match='parent sync failed'):
        executor.delete_playlist(store, source, client, 3)
    assert backups.is_dir()
    assert client.deleted == []
    assert store.actions.get_actions() == []
    assert store.playlists.get_playlist(source) is not None

    executor.delete_playlist(store, source, client, 4)
    assert attempts == [target, target]
    assert client.deleted == ['SOURCE']
    action, = store.actions.get_actions()
    assert action.status == 'executed'


@pytest.mark.parametrize("contender", ["manual", "empty", "generated", "planned"])
def test_deletion_admission_covers_lookup_backup_and_transport_across_stores(deletion, monkeypatch, contender):
    store, database, identity, source, keeper, client = deletion
    store.playlists.set_playlist_group("SOURCE", "Generated")
    store.playlists.set_playlist_provenance("SOURCE", origin="tuneconsole", adopted=False)
    if contender == "planned":
        aid = executor.store_plan(store, executor.MergePlan(source, keeper, [], []), "delete", "SOURCE", 2)
    entered, release = threading.Event(), threading.Event()
    original = executor.backup_playlist

    def pause_backup(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(executor, "backup_playlist", pause_backup)
    reader = _reopen(database)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(executor.delete_playlist, store, source, client, 3)
            try:
                assert entered.wait(5)
                assert reader.actions.get_pending_deletion(identity, "SOURCE") is None
                if contender == "generated":
                    assert playlist_generated.gc_generated_playlists(reader, {identity: client}, 10**9, grace_days=0) == []
                else:
                    with pytest.raises(ValueError, match="already running for this playlist"):
                        if contender == "manual":
                            executor.delete_playlist(reader, source, client, 4)
                        elif contender == "empty":
                            executor.delete_empty_playlist(reader, source, client, 4)
                        else:
                            executor.execute_planned(reader, aid, {identity: client}, 4)
            finally:
                release.set()
            first.result(timeout=5)
        owners = [a for a in reader.actions.get_actions() if json.loads(a.undo_json).get("deletions")]
        owner, = owners
        assert client.deleted == ["SOURCE"]
        playlist_undo.undo_action(reader, owner.id, {identity: client}, 5)
        assert len(client.created) == 1
    finally:
        reader.conn.close()


@pytest.mark.parametrize("operation", ["manual", "empty", "planned", "generated"])
def test_backup_and_intent_are_committed_before_transport_and_survive_lost_reply(deletion, operation):
    store, database, identity, source, keeper, client = deletion
    if operation == "empty":
        client._tracks["SOURCE"] = []
    elif operation == "generated":
        store.playlists.set_playlist_group("SOURCE", "Generated")
        store.playlists.set_playlist_provenance("SOURCE", origin="tuneconsole", adopted=False)
    original = client.delete_playlist

    def lose_reply(playlist_id):
        reader = _reopen(database)  # another process can discover the intent before the write
        try:
            action, = reader.actions.get_actions()
            assert action.status == "delete_pending"
            undo = json.loads(action.undo_json)
            entry, = undo["deletions"]
            assert entry["phase"] == "pending" and entry["ytm"] == playlist_id
            backup = json.loads(Path(entry["backup"]).read_text())
            assert backup["ytm_playlist_id"] == playlist_id
            assert [t["video_id"] for t in backup["tracks"]] == ([] if operation == "empty" else ["a", "b"])
        finally:
            reader.conn.close()
        original(playlist_id)
        raise TimeoutError("Deletion reply lost")

    client.delete_playlist = lose_reply
    if operation == "planned":
        aid = executor.store_plan(store, executor.MergePlan(source, keeper, [], []), "delete", "SOURCE", 2)
        run = lambda: executor.execute_planned(store, aid, {identity: client}, 3)
    elif operation == "empty":
        run = lambda: executor.delete_empty_playlist(store, source, client, 3)
    elif operation == "generated":
        run = lambda: playlist_generated.gc_generated_playlists(store, {identity: client}, 10 * 86400, grace_days=1)
    else:
        run = lambda: executor.delete_playlist(store, source, client, 3)
    if operation == "generated":
        assert run() == []
    else:
        with pytest.raises(TimeoutError, match="reply lost"):
            run()
    action, = store.actions.get_actions()
    assert action.status == "delete_pending"
    reopened = _reopen(database)
    try:
        playlist_deletion.reconcile_deletion(reopened, action.id, {identity: client}, 4)
        assert reopened.actions.get_action(action.id).status == "executed"
        assert reopened.playlists.get_playlist(source) is None
        playlist_undo.undo_action(reopened, action.id, {identity: client}, 5)
        assert reopened.actions.get_action(action.id).status == "undone"
        assert len(client.created) == 1
        assert client.deleted == ["SOURCE"]
    finally:
        reopened.conn.close()


@pytest.mark.parametrize("failure", ["local_database", "completion_record"])
def test_restart_after_remote_delete_recovers_local_or_completion_failure(deletion, monkeypatch, failure):
    store, database, identity, source, _, client = deletion
    with monkeypatch.context() as patch:
        if failure == "local_database":
            def fail(*args, **kwargs):
                raise OSError("Local database unavailable")
            patch.setattr(store.playlists, "remove_playlist", fail)
        else:
            update = store.actions.update_action

            def fail(action_id, status, *args, **kwargs):
                if status == "executed":
                    raise SystemExit("Process interrupted before completion recording")
                return update(action_id, status, *args, **kwargs)
            patch.setattr(store.actions, "update_action", fail)
        with pytest.raises((OSError, SystemExit)):
            executor.delete_playlist(store, source, client, 2)
    action, = store.actions.get_actions()
    assert action.status == "delete_pending"
    assert json.loads(action.undo_json)["deletions"][0]["phase"] == "remote_complete"
    reopened = _reopen(database)
    try:
        playlist_deletion.reconcile_deletion(reopened, action.id, {identity: client}, 3)
        assert reopened.actions.get_action(action.id).status == "executed"
        assert reopened.playlists.get_playlist(source) is None
        assert client.deleted == ["SOURCE"]
    finally:
        reopened.conn.close()


def test_repeated_manual_request_reconciles_lost_reply_without_deleting_again(deletion):
    store, _, _, source, _, client = deletion
    client.lose_delete = True
    with pytest.raises(TimeoutError):
        executor.delete_playlist(store, source, client, 2)
    backup = executor.delete_playlist(store, source, client, 3)
    assert Path(backup).is_file()
    assert len(store.actions.get_actions()) == 1 and client.deleted == ["SOURCE"]


def test_collection_sees_intent_created_mid_sweep_without_loading_history(deletion, monkeypatch):
    store, _, identity, source, keeper, client = deletion
    for ytm in ("SOURCE", "KEEP"):
        store.playlists.set_playlist_group(ytm, "Generated")
        store.playlists.set_playlist_provenance(ytm, origin="tuneconsole", adopted=False)
    original = client.delete_playlist

    def delete(ytm):
        result = original(ytm)
        if ytm == "KEEP":
            raise TimeoutError("Deletion reply lost")
        # A different request starts deleting the next playlist during this sweep.
        with pytest.raises(TimeoutError, match="reply lost"):
            executor.delete_playlist(store, keeper, client, 2)
        return result

    def no_history():
        pytest.fail("Deletion admission must not load the full action log")

    monkeypatch.setattr(client, "delete_playlist", delete)
    monkeypatch.setattr(store.actions, "get_actions", no_history)

    collected = playlist_generated.gc_generated_playlists(
        store, {identity: client}, 10 * 86400, grace_days=1)

    assert {item["ytm"] for item in collected} == {"SOURCE", "KEEP"}
    assert all(Path(item["backup"]).is_file() for item in collected)
    assert client.deleted == ["SOURCE", "KEEP"]
    assert store.playlists.get_playlist(source) is None
    assert store.playlists.get_playlist(keeper) is None
    assert store.actions.get_pending_deletion(identity, "KEEP") is None


def test_unreadable_outcome_blocks_undo_then_preserves_surviving_original(deletion, monkeypatch):
    store, _, identity, source, _, client = deletion

    def lose_request(playlist_id):
        raise TimeoutError("No reply; request may not have reached YouTube")

    monkeypatch.setattr(client, "delete_playlist", lose_request)
    with pytest.raises(TimeoutError):
        executor.delete_playlist(store, source, client, 2)
    action, = store.actions.get_actions()
    with monkeypatch.context() as patch:
        def unreadable(*args, **kwargs):
            raise ConnectionError("Disconnected")
        patch.setattr(client, "get_playlist", unreadable)
        with pytest.raises(ValueError, match="still unconfirmed"):
            playlist_undo.undo_action(store, action.id, {identity: client}, 3)
        assert store.actions.get_action(action.id).status == "delete_pending"
    result = playlist_deletion.reconcile_deletion(store, action.id, {identity: client}, 4)
    assert result.status == "delete_failed"
    assert store.playlists.get_playlist(source) is not None
    playlist_undo.undo_action(store, action.id, {identity: client}, 5)
    assert client.created == [] and client.deleted == []
    assert store.actions.get_action(action.id).status == "undone"


def test_failed_intent_commit_never_authorizes_transport(deletion, monkeypatch):
    store, _, _, source, _, client = deletion

    def unavailable(*args, **kwargs):
        raise OSError("Intent commit failed")

    monkeypatch.setattr(store.actions, "record_action", unavailable)
    with pytest.raises(OSError):
        executor.delete_playlist(store, source, client, 2)
    assert client.deleted == [] and store.playlists.get_playlist(source) is not None


def test_merge_interruption_keeps_one_owner_with_keeper_and_source_recovery(deletion, monkeypatch):
    store, database, identity, source, keeper, client = deletion

    def interrupted(playlist_id):
        raise SystemExit("Process died in transport")

    monkeypatch.setattr(client, "delete_playlist", interrupted)
    with pytest.raises(SystemExit):
        executor.apply_result(store, {identity: client}, [keeper, source], ["a", "b"], keeper, 2)
    reopened = _reopen(database)
    try:
        action, = reopened.actions.get_actions()
        assert action.kind == "apply_merge" and action.status == "delete_pending"
        undo = json.loads(action.undo_json)
        assert undo["restored"][0]["prev"] == ["a", "b"]
        assert len(undo["backups"]) == len(undo["deletions"]) == 1
    finally:
        reopened.conn.close()


def test_action_log_offers_honest_pending_status_recheck_and_undo(deletion):
    store, _, identity, source, _, client = deletion
    client.lose_delete = True
    with pytest.raises(TimeoutError):
        executor.delete_playlist(store, source, client, 2)
    action, = store.actions.get_actions()
    with TestClient(create_app(store, lambda: {identity: client}, now_fn=lambda: 3),
                    base_url="http://127.0.0.1") as web:
        html = web.get("/actions").text
        assert "deletion pending" in html and "The backup is saved" in html
        assert f'/actions/{action.id}/recheck-deletion' in html and f'/undo/{action.id}' in html
        result = web.post(f"/actions/{action.id}/recheck-deletion", follow_redirects=False)
        assert result.status_code == 303
        assert store.actions.get_action(action.id).status == "executed"
        assert client.deleted == ["SOURCE"]
