"""Bulk identity transfers validate selections and preserve individual playlist outcomes."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient, _track
from yt_playlist.web.app import create_app


@pytest.fixture
def transfers(store, monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    source = store.upsert_identity("Main", "c1", None, True)
    target = store.upsert_identity("Alt", "c2", None, False)
    playlists = []
    tracks = {}
    for n, title in enumerate(("Evening", "Mix", "Weekend")):
        ytm = f"PL{n}"
        pid = store.upsert_playlist(source, ytm, title, 1, "h", 1.0)
        tid = store.upsert_track(f"v{n}", f"Song {n}", "Artist", None, 200, 1)
        store.set_playlist_tracks(pid, [tid])
        tracks[ytm] = [_track(f"v{n}", f"Song {n}", "Artist")]
        playlists.append(pid)
    other = store.upsert_playlist(target, "PL_ALT", "Already here", 0, "h", 1.0)
    src_client = FakeClient(tracks=tracks)
    tgt_client = FakeClient(catalog={t[0]["videoId"]: t[0] for t in tracks.values()})
    clients = {source: src_client, target: tgt_client}
    refreshes = []
    bridge = SimpleNamespace(send_control=refreshes.append)
    app = create_app(store, lambda: clients, now_fn=lambda: 1.0, bridge=bridge)
    return SimpleNamespace(store=store, source=source, target=target, playlists=playlists,
                           other=other, src_client=src_client, tgt_client=tgt_client,
                           refreshes=refreshes, client=TestClient(app, base_url="http://127.0.0.1"))


def _transfer(t, ids=None, *, copy=False, **overrides):
    data = {"ids": ",".join(map(str, t.playlists if ids is None else ids)),
            "source_identity": t.source, "target_identity": t.target,
            "copy_only": "1" if copy else ""}
    return t.client.post("/move/bulk", data={**data, **overrides})


def test_bulk_copy_keeps_separate_playlists_and_originals(transfers):
    t = transfers
    response = _transfer(t, [t.playlists[0], t.playlists[2], t.playlists[0]], copy=True)

    assert response.status_code == 200
    assert "Copied 2 playlists." in response.text
    assert [p[1] for p in t.tgt_client.created] == ["Evening", "Weekend"]
    assert all(t.store.get_playlist(pid) for pid in t.playlists)
    copies = [p for p in t.store.get_playlists() if p.identity_id == t.target and p.id != t.other]
    assert [(p.title, p.track_count) for p in copies] == [("Evening", 1), ("Weekend", 1)]
    assert t.src_client.deleted == []
    assert t.refreshes == []


def test_bulk_move_deletes_only_selected_and_backs_up_each(transfers):
    t = transfers
    response = _transfer(t, t.playlists[:2])

    assert "Moved 2 playlists." in response.text
    assert all(t.store.get_playlist(pid) is None for pid in t.playlists[:2])
    assert t.store.get_playlist(t.playlists[2]) is not None
    assert t.src_client.deleted == ["PL0", "PL1"]
    assert t.refreshes == [{"type": "refresh-view", "playlist": pid} for pid in ("PL0", "PL1")]
    actions = t.store.get_actions()
    assert len(actions) == 2
    for action in actions:
        backup = json.loads(action.undo_json)["backup"]
        assert Path(backup).is_file()
        assert len(json.loads(Path(backup).read_text())["tracks"]) == 1


@pytest.mark.parametrize("invalid", ["99999", "other", "bad", " "])
def test_invalid_selection_is_rejected_before_any_copy(transfers, invalid):
    t = transfers
    ids = str(t.other) if invalid == "other" else invalid
    response = _transfer(t, ids=f"{t.playlists[0]},{ids}".split(",") if invalid != " " else [" "])

    assert response.status_code == 200
    assert response.context["error"]
    assert not response.context["results"]
    assert not t.tgt_client.created
    assert not t.src_client.deleted


def test_removed_target_is_rejected_even_from_a_stale_page(transfers):
    t = transfers
    t.store.conn.execute("UPDATE identities SET active=0 WHERE id=?", (t.target,))
    response = _transfer(t)
    assert "An identity has been removed" in response.text
    assert not t.tgt_client.created


def test_same_identity_is_rejected(transfers):
    t = transfers
    response = _transfer(t, target_identity=t.source)
    assert "Pick two different identities" in response.text
    assert not t.tgt_client.created


def test_one_failure_does_not_hide_or_prevent_other_results(transfers, monkeypatch):
    t = transfers
    create = t.tgt_client.create_playlist

    def fail_mix(title, description):
        if title == "Mix":
            raise ValueError("This playlist could not be created.")
        return create(title, description)

    monkeypatch.setattr(t.tgt_client, "create_playlist", fail_mix)
    response = _transfer(t)

    assert "Moved 2 playlists. 1 failed." in response.text
    assert "This playlist could not be created." in response.text
    assert '<details open>' in response.text
    assert t.store.get_playlist(t.playlists[1]) is not None
    assert t.src_client.deleted == ["PL0", "PL2"]


def test_incomplete_move_keeps_source_and_reports_copy(transfers):
    t = transfers
    missing = t.store.upsert_track(None, "Unfindable song", "Unknown artist", None, 200, 1)
    t.store.set_playlist_tracks(t.playlists[1], [missing])
    response = _transfer(t, t.playlists[:2])

    assert "Moved 1 playlist. Copied 1 playlist." in response.text
    assert "1 couldn’t be matched" in response.text
    assert "Source kept" in response.text
    assert t.store.get_playlist(t.playlists[1]) is not None
    assert t.src_client.deleted == ["PL0"]


def test_delete_failure_reports_kept_source_without_losing_copy(transfers, monkeypatch):
    t = transfers

    def refuse_delete(_playlist):
        raise ValueError("YouTube refused")

    monkeypatch.setattr(t.src_client, "delete_playlist", refuse_delete)
    response = _transfer(t, t.playlists[:1])
    assert "Copied 1 playlist." in response.text
    assert "Source kept: YouTube couldn’t delete the original." in response.text
    assert t.store.get_playlist(t.playlists[0]) is not None
    assert len(t.tgt_client.created) == 1
    assert not t.refreshes
