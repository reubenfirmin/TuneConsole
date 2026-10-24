"""Facade Undo keeps recovery's local updates without unrelated refreshes or false errors."""
import logging

import pytest

from tests.test_undo_recovery import RecoveryClient, _copy, _deleted
from yt_playlist.library.ops import PlaylistOps


@pytest.mark.parametrize("operation", ["delete", "copy", "planned_delete"])
def test_successful_facade_undo_updates_local_membership_without_errors_or_keeper_reads(
    store, tmp_path, monkeypatch, caplog, operation,
):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    client = RecoveryClient({"SOURCE": ["a", "b"], "KEEP": ["a", "b"], "COPY": ["a", "b"]})
    if operation == "copy":
        iid, aid = _copy(store, client)
    elif operation == "delete":
        iid, aid = _deleted(store, client)
    else:
        iid = store.identities.upsert_identity("Main", "bridge", None, True)
        source = store.playlists.upsert_playlist(iid, "SOURCE", "Source", 2, "", 1)
        keeper = store.playlists.upsert_playlist(iid, "KEEP", "Keeper", 2, "", 1)
        PlaylistOps(store, lambda: {iid: client}, lambda: 2).delete_dupe(source, keeper)
        aid = store.actions.get_actions()[0].id
    reads = []
    get = client.get_playlist

    def read(pid, limit=None):
        reads.append(pid)
        return get(pid, limit)

    monkeypatch.setattr(client, "get_playlist", read)
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        PlaylistOps(store, lambda: {iid: client}, lambda: 3).undo(aid)
    assert not caplog.records
    assert "KEEP" not in reads
    assert store.actions.get_action(aid).status == "undone"
    if operation == "copy":
        assert not store.playlists.get_playlists()
    else:
        restored = next(p for p in store.playlists.get_playlists() if p.ytm_playlist_id == "PL_NEW_0")
        assert [row[1] for row in store.playlists.get_playlist_tracks_with_meta(restored.id)] == ["a", "b"]
