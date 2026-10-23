"""Bulk actions report outcomes that remain inspectable after navigation/reload."""
import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from yt_playlist.library.ops import PlaylistOps
from yt_playlist.web.app import create_app


@pytest.fixture
def library(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    first = store.playlists.upsert_playlist(iid, "PLA", "Alpha", 0, "", 0)
    second = store.playlists.upsert_playlist(iid, "PLB", "Beta", 0, "", 0)
    client = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1),
                        base_url="http://127.0.0.1")
    return client, first, second


@pytest.mark.parametrize("failed_ids", [[0], [0, 1]])
def test_failed_and_mixed_delete_batches_report_each_item(store, library, monkeypatch, failed_ids):
    client, first, second = library
    ids = [first, second]
    calls = []
    def delete(self, pid):
        calls.append(pid)
        if pid in [ids[i] for i in failed_ids]:
            raise ValueError("Delete rejected")
        store.playlists.remove_playlist(pid)
    monkeypatch.setattr(PlaylistOps, "delete", delete)
    response = client.post("/playlists/delete", data={"ids": f"{first},{second},{first}"})
    result = response.json()
    assert response.status_code == (422 if len(failed_ids) == 2 else 200)
    assert result["status"] == ("failed" if len(failed_ids) == 2 else "partial")
    assert [item["status"] for item in result["items"]] == (["failed", "failed"] if len(failed_ids) == 2 else ["failed", "deleted"])
    assert calls == ids
    assert "hx-refresh" not in response.headers
    for _ in range(2):
        page = client.get(response.headers["hx-redirect"])
        assert page.status_code == 200
        assert "Alpha" in page.text and "Beta" in page.text and "Delete rejected" in page.text
        assert "Check YouTube before retrying failed items" in page.text
    assert calls == ids  # refreshing the result never repeats a deletion


@pytest.mark.parametrize("skipped", [0, 1])
def test_copy_counts_destination_and_missing_songs_survive_refresh(library, monkeypatch, skipped):
    client, first, _ = library
    monkeypatch.setattr(PlaylistOps, "copy", lambda *args: {"title": "New copy", "new_ytm": "NEW",
        "added": 2, "skipped": skipped, "skipped_video_ids": ["missing-song"] if skipped else []})
    response = client.post("/playlists/copy", data={"ids": str(first), "name": "New copy"})
    assert response.json()["status"] == ("partial" if skipped else "complete")
    page = client.get(response.headers["hx-redirect"])
    assert "Copied 2 track(s) to New copy" in page.text
    assert 'https://music.youtube.com/playlist?list=NEW' in page.text
    if skipped:
        assert "missing-song" in page.text and "Repeating Copy creates another playlist" in page.text


def test_copy_exception_is_a_failure_with_uncertainty_and_recovery_guidance(library, monkeypatch):
    client, first, _ = library
    def fail(*args):
        raise RuntimeError("Connection lost")
    monkeypatch.setattr(PlaylistOps, "copy", fail)
    response = client.post("/playlists/copy", data={"ids": str(first)})
    assert response.status_code == 422 and response.json()["status"] == "failed"
    page = client.get(response.headers["hx-redirect"])
    assert "Alpha" in page.text and "Connection lost" in page.text
    assert "Check YouTube for a newly created playlist before retrying" in page.text


def test_cleanup_cache_failure_does_not_hide_successful_deletion(store, library, monkeypatch):
    from yt_playlist.rec import actions
    client, first, _ = library
    monkeypatch.setattr(PlaylistOps, "delete", lambda self, pid: store.playlists.remove_playlist(pid))
    monkeypatch.setattr(actions, "refresh_cleanup", lambda *args: (_ for _ in ()).throw(RuntimeError("cache failure")))
    response = client.post("/playlists/delete", data={"ids": str(first)})
    assert response.json()["status"] == "complete"
    assert response.json()["items"][0]["status"] == "deleted"
