"""Contract tests for the htmx playlist-detail routes (rename / track-year / track-genre /
remove-track / reorder / alternates / add-tracks / lastfm-key).

These routes now consume form data and return _partials fragments (or HX-Refresh), instead of
the old JSON payloads. Coverage moved here from the JSON-based tests in test_web.py as each
route is converted.
"""
from fastapi.testclient import TestClient

from yt_playlist.web.app import create_app
from tests.conftest import FakeClient, _track


def _client(store, provider):
    return TestClient(create_app(store, provider, now_fn=lambda: 1.0), base_url="http://127.0.0.1")


# --- rename ---

def test_rename_returns_head_fragment(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PL1", "Old Name", 1, "h", 1.0)
    store.set_playlist_tracks(a, [store.upsert_track("v0", "S", "X", "Al", 200, 1)])
    fc = FakeClient()
    c = _client(store, lambda: {iid: fc})

    r = c.post(f"/playlist/{a}/rename", data={"title": "  New Name  "})
    assert r.status_code == 200
    assert "<!doctype html>" not in r.text.lower()        # a fragment, not the whole page
    assert "New Name" in r.text and 'id="pl-title"' in r.text
    assert store.get_playlist(a).title == "New Name"       # trimmed + persisted
    assert fc.edited == [("PL1", {"title": "New Name"})]   # pushed to YouTube


def test_rename_empty_returns_toast(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PL1", "Keep", 1, "h", 1.0)
    store.set_playlist_tracks(a, [store.upsert_track("v0", "S", "X", "Al", 200, 1)])
    c = _client(store, lambda: {iid: FakeClient()})

    r = c.post(f"/playlist/{a}/rename", data={"title": "   "})
    assert r.status_code == 422
    assert r.headers.get("hx-reswap") == "none"
    assert 'hx-swap-oob="afterbegin:#toasts"' in r.text
    assert store.get_playlist(a).title == "Keep"           # unchanged
