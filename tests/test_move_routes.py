"""Cross-identity playlist copy and move route responses."""
import json
from pathlib import Path

import pytest

from tests.conftest import FakeClient, _track
from tests.web_support import _client
def _two_identity_move(store):
    i1 = store.accounts.upsert_account("Main", "c1", None, True)
    i2 = store.accounts.upsert_account("Alt", "c2", None, False)
    pid = store.playlists.upsert_playlist(i1, "PL1", "Mix", 1, "h", 1.0)
    t = store.tracks.upsert_track("v1", "S", "X", None, None, 1)
    store.playlists.set_playlist_tracks(pid, [t])
    return i1, i2, pid


def test_move_run_copy_returns_row_with_message(store):
    i1, i2, pid = _two_identity_move(store)
    c = _client(store, lambda: {i1: FakeClient(), i2: FakeClient()})
    r = c.post("/move/run", data={"playlist": pid, "target_identity": i2, "copy_only": "1"})
    assert r.status_code == 200
    assert "Copied" in r.text and "Mix" in r.text       # row re-rendered, still present
    assert store.playlists.get_playlist(pid) is not None


def test_move_run_move_removes_row(store,monkeypatch,tmp_path):
    monkeypatch.setenv('YT_PLAYLIST_HOME',str(tmp_path))
    i1, i2, pid = _two_identity_move(store)
    source = FakeClient(tracks={"PL1": [_track("v1", "S", "X")]})
    target = FakeClient(catalog={"v1": _track("v1", "S", "X")})
    c = _client(store, lambda: {i1: source, i2: target})
    r = c.post("/move/run", data={"playlist": pid, "target_identity": i2})   # move (no copy_only)
    assert r.status_code == 200
    assert r.text.strip() == ""                          # deleted -> empty -> htmx removes the row
    assert store.playlists.get_playlist(pid) is None


def test_move_run_same_identity_shows_inline_error(store):
    i1, _i2, pid = _two_identity_move(store)
    c = _client(store, lambda: {i1: FakeClient()})
    r = c.post("/move/run", data={"playlist": pid, "target_identity": i1})
    assert r.status_code == 200
    assert "same" in r.text.lower() and "move-row" in r.text   # error re-rendered in the row


def test_move_run_gone_playlist_drops_row(store):
    # the playlist was deleted elsewhere between page load and click -> ops().move raises,
    # and the row must drop cleanly (empty fragment) rather than render a None-deref'd row.
    i1, i2, _pid = _two_identity_move(store)
    c = _client(store, lambda: {i1: FakeClient(), i2: FakeClient()})
    r = c.post("/move/run", data={"playlist": "99999", "target_identity": i2})   # no such playlist
    assert r.status_code == 200 and r.text.strip() == ""


@pytest.mark.parametrize("endpoint", ["/move/run", "/move/bulk"])
def test_move_delete_failure_retains_copy_and_recovery_without_diagnostic(
    store, monkeypatch, tmp_path, endpoint
):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    source_id, target_id, playlist_id = _two_identity_move(store)
    source = FakeClient(tracks={"PL1": [_track("v1", "S", "X")]})
    target = FakeClient(catalog={"v1": _track("v1", "S", "X")})
    diagnostic = "sqlite3.OperationalError: review879-private-move-diagnostic"
    attempts = []

    def uncertain_delete(remote_id):
        attempts.append(remote_id)
        raise RuntimeError(diagnostic)

    monkeypatch.setattr(source, "delete_playlist", uncertain_delete)
    values = {"playlist": playlist_id, "target_identity": target_id}
    if endpoint == "/move/bulk":
        values = {"ids": str(playlist_id), "source_identity": source_id,
                  "target_identity": target_id, "copy_only": ""}
    with _client(store, lambda: {source_id: source, target_id: target}) as client:
        response = client.post(endpoint, data=values)
        reloaded = client.get("/move")

    # The actual transfer, verification, backup and deletion journal run. Only the
    # provider's delete response fails, after the destination has been retained.
    assert response.status_code == 200
    assert reloaded.status_code == 200
    assert store.playlists.get_playlist(playlist_id) is not None
    copies = [p for p in store.playlists.get_playlists() if p.account_id == target_id]
    assert len(copies) == 1 and copies[0].track_count == 1
    assert len(target.created) == 1
    assert attempts == ["PL1"]  # Rendering and reloading must not replay the write.
    actions = store.actions.get_actions()
    assert len(actions) == 1 and actions[0].status == "delete_pending"
    deletion = json.loads(actions[0].undo_json)["deletions"][0]
    assert deletion["ytm"] == "PL1" and deletion["phase"] == "uncertain"
    assert Path(deletion["backup"]).is_file()
    assert "cop" in response.text.lower()
    assert diagnostic not in response.text
    assert "OperationalError" not in response.text
    assert diagnostic not in reloaded.text


