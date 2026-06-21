import json
from fastapi.testclient import TestClient

from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def _seed(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_track("v1", "Anchor", "Band", None, None)
    b = store.upsert_track("v2", "Bonus", "Band", None, None)
    target = store.upsert_playlist(iid, "PT", "Target", 1, "h", 0.0)
    store.set_playlist_tracks(target, [a])
    other = store.upsert_playlist(iid, "PO", "Other", 2, "h2", 0.0)
    store.set_playlist_tracks(other, [a, b])
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0)
    return target, TestClient(app, base_url="http://127.0.0.1")


def test_playlist_suggestions_fragment_renders_fits(store):
    target, c = _seed(store)
    r = c.get(f"/playlist/{target}/suggestions")
    assert r.status_code == 200
    assert "Complete this playlist" in r.text
    assert "Bonus" in r.text                       # a fitting owned track


def test_playlist_suggestions_unknown_id_404(store):
    _, c = _seed(store)
    assert c.get("/playlist/999999/suggestions").status_code == 404


def test_playlist_page_lazy_loads_suggestions(store):
    target, c = _seed(store)
    assert f"/playlist/{target}/suggestions" in c.get(f"/playlist/{target}").text


def test_suggestion_card_has_wired_add_button(store):
    target, c = _seed(store)
    frag = c.get(f"/playlist/{target}/suggestions").text
    assert f"/playlist/{target}/add-tracks" in frag    # Add posts to the existing endpoint
    assert 'name="track"' in frag and "+ Add" in frag and "v2" in frag


def test_recs_rebuild_endpoint(store):
    store.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {}, now_fn=lambda: 1.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    r = c.post("/recs/rebuild")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_add_suggested_track_adds_it_to_the_playlist(store):
    target, c = _seed(store)
    # what the Add button POSTs for the "Bonus" suggestion (videoId v2): a 'track' form field
    r = c.post(f"/playlist/{target}/add-tracks",
               data={"track": json.dumps({"videoId": "v2", "title": "Bonus", "artist": "Band"})})
    assert r.status_code == 200
    assert "bonus|band" in store.get_playlist_track_keys(target)
