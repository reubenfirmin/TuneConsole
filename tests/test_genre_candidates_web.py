"""#103 per-song genre candidate provenance and selection."""
import time

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
    assert "Hyperballad" in html and "Bjork" in html
    assert "Electronic" in html and "musicbrainz" in html
    assert "Art Pop" in html and "discogs" in html
    assert "Apply genre" in html and "Current" in html
    assert "Use custom genre" not in html


def test_provider_agreement_is_one_genre_choice(store):
    client, iid = _client(store)
    _pid, tid = _seed(store, iid)
    store.log_enrichment(tid, "later", "lastfm", "genre", "Electronic", now=2.0)
    provenance = store.genre_provenance(tid)
    electronic = next(o for o in provenance["options"] if o["value"] == "Electronic")
    assert electronic["providers"] == ["musicbrainz", "lastfm"]
    html = client.get(f"/track/{tid}/genre-candidates").text
    assert html.count('value="Electronic"') == 1


def test_single_genre_choice_has_no_redundant_labels(store):
    client, iid = _client(store)
    pid = store.upsert_playlist(iid, "PL1", "Mix", 1, "h", 1.0)
    tid = store.upsert_track("v1", "Song", "Artist", None, 200)
    store.set_playlist_tracks(pid, [tid])
    store.set_track_genre(tid, "Ambient")
    store.log_enrichment(tid, "run", "discogs", "genre", "Ambient", now=1.0)
    html = client.get(f"/track/{tid}/genre-candidates").text
    assert ">Suggestions<" not in html
    assert ">Current<" not in html


def test_empty_dialog_offers_single_track_lookup(store, monkeypatch):
    client, iid = _client(store)
    pid = store.upsert_playlist(iid, "PL1", "Mix", 1, "h", 1.0)
    tid = store.upsert_track("v1", "Unknown", "Artist", None, 200)
    store.set_playlist_tracks(pid, [tid])
    html = client.get(f"/track/{tid}/genre-candidates").text
    assert "No genre suggestions yet" in html
    assert 'aria-label="Look up genre metadata"' in html

    def lookup(_store, tracks, _config, _progress):
        _store.log_enrichment(tracks[0]["id"], "lookup", "discogs", "genre", "Ambient", now=2.0)

    monkeypatch.setattr("yt_playlist.web.routes.enrich.waterfall.run_waterfall", lookup)
    response = client.post(f"/track/{tid}/genre-lookup")
    assert response.status_code == 200 and response.json()["job_id"]
    job = client.app.state.ctx.jobs.get(response.json()["job_id"])
    deadline = time.time() + 1
    while not job.done and time.time() < deadline:
        time.sleep(.01)
    assert job.done
    html = client.get(f"/track/{tid}/genre-candidates").text
    assert "Ambient" in html and "discogs" in html
    assert "/track/genre-lookup/events/" in (open("src/yt_playlist/web/static/app.js").read())


def test_candidate_choice_updates_canonical_genre_and_refreshes(store):
    client, iid = _client(store)
    _pid, tid = _seed(store, iid)
    response = client.post(f"/track/{tid}/genre-candidates", data={"genre": "Art Pop"})
    assert response.status_code == 204
    assert response.headers["HX-Refresh"] == "true"
    assert store.genre_provenance(tid)["current"] == "Art Pop"


def test_custom_genre_is_accepted_and_empty_value_rejected(store):
    client, iid = _client(store)
    _pid, tid = _seed(store, iid)
    assert client.post(f"/track/{tid}/genre-candidates", data={"genre": "Dream Metal"}).status_code == 204
    assert store.genre_provenance(tid)["current"] == "Dream Metal"
    assert client.post(f"/track/{tid}/genre-candidates", data={"genre": ""}).status_code == 400
