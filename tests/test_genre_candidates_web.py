"""#103 per-song genre candidate provenance and selection."""
from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from yt_playlist.web.app import create_app


def _client(store):
    iid = store.upsert_identity("main", "cred", None, True)
    return TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0),
                      base_url="http://127.0.0.1"), iid


def _seed(store, iid):
    pid = store.upsert_playlist(iid, "PL1", "Mix", 1, "h", 1.0)
    tid = store.upsert_track("v1", "Hyperballad", "Bjork", "Post", 200)
    store.set_playlist_tracks(pid, [tid])
    store.set_track_enrichment(tid, "Electronic", "1995")
    store.log_enrichment(tid, "run", "musicbrainz", "genre", "Electronic", now=1.0)
    store.log_enrichment(tid, "run", "discogs", "genre", "Art Pop", now=1.0)
    return pid, tid


def test_playlist_genre_cell_links_to_candidate_inspector(store):
    client, iid = _client(store)
    pid, tid = _seed(store, iid)
    html = client.get(f"/playlist/{pid}").text
    assert f'hx-get="/track/{tid}/genre-candidates"' in html
    assert 'hx-target="#genre-candidates-modal"' in html


def test_candidate_inspector_shows_sources_and_current_value(store):
    client, iid = _client(store)
    _pid, tid = _seed(store, iid)
    html = client.get(f"/track/{tid}/genre-candidates").text
    assert "Hyperballad" in html and "Currently used" in html
    assert "Electronic" in html and "musicbrainz" in html
    assert "Art Pop" in html and "discogs" in html
    assert "Use this" in html and "Current" in html


def test_candidate_choice_updates_canonical_genre_and_refreshes(store):
    client, iid = _client(store)
    _pid, tid = _seed(store, iid)
    response = client.post(f"/track/{tid}/genre-candidates", data={"genre": "Art Pop"})
    assert response.status_code == 204
    assert response.headers["HX-Refresh"] == "true"
    assert store.genre_provenance(tid)["current"] == "Art Pop"


def test_candidate_choice_rejects_unretained_value(store):
    client, iid = _client(store)
    _pid, tid = _seed(store, iid)
    assert client.post(f"/track/{tid}/genre-candidates", data={"genre": "Metal"}).status_code == 400
