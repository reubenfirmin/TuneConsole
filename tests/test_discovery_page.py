"""Task 7 (#53): the Tools > Discovery Pools page lists the pools and the add-to-collection (Like)
action likes a discovered track."""
from fastapi.testclient import TestClient

from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def _client(store):
    iid = store.upsert_identity("main", "cred", None, True)
    fake = FakeClient()
    return fake, TestClient(create_app(store, lambda: {iid: fake}, now_fn=lambda: 1000.0),
                            base_url="http://127.0.0.1")


def test_discovery_page_lists_pooled_track(store):
    store.upsert_discovered_track("a|x", "v1", "Cold Song", "Artist", "Alb", None, "house", "2020",
                                  "r", 100.0)
    store.mark_offered("track", ["a|x"], 200.0)
    _, c = _client(store)
    r = c.get("/discovery")
    assert r.status_code == 200
    assert "Cold Song" in r.text


def test_discovery_add_likes_track(store):
    store.upsert_discovered_track("a|x", "v1", "Cold Song", "Artist", "Alb", None, None, None, "r", 100.0)
    fake, c = _client(store)
    r = c.post("/discovery/add", data={"kind": "track", "id": "a|x"})
    assert r.status_code == 200
    assert ("v1", "LIKE") in fake.rated      # rated thumbs-up -> Liked Music


def test_fragment_loads_do_not_count_as_album_or_artist_views(store):
    store.upsert_discovered_album("album", "Artist", "Unseen album", "2026", None, 100.0)
    store.upsert_discovered_artist("New artist", 1.0, [], [], None, 100.0)
    _, c = _client(store)
    for _ in range(3):
        assert c.get("/home/discover").status_code == 200
        assert c.get("/home/new-artists").status_code == 200
    assert store.get_discovered_albums()[0]["offered_count"] == 0
    assert store.get_discovered_artists()[0]["offered_count"] == 0
    assert "less than 1d ago" in c.get("/discovery").text


def test_visible_tile_endpoint_counts_once_and_validates_kind(store):
    from uuid import uuid4

    store.upsert_discovered_album("album", "Artist", "Album", "2026", None, 100.0)
    _, c = _client(store)
    payload = {"kind": "album", "ids": ["album"], "view_id": str(uuid4())}
    assert c.post("/discovery/impressions", json=payload).status_code == 204
    assert c.post("/discovery/impressions", json=payload).status_code == 204
    assert store.get_discovered_albums()[0]["offered_count"] == 1
    payload["kind"] = "anything"
    assert c.post("/discovery/impressions", json=payload).status_code == 422


def test_saved_metadata_is_visible_without_queue_or_provider_details(store):
    store.upsert_discovered_track("cold", "v", "Cold Song", "Artist", "Album", None,
                                  "House", "2020", "album", 100,
                                  audio={"bpm": 128.0, "energy": .8, "danceability": .7})
    store.record_enrichment_attempt("cold", "musicbrainz", {"year": "2020"}, now=200)
    store.record_enrichment_attempt("cold", "deezer", {}, now=201)
    store.record_enrichment_attempt("cold", "acousticbrainz", {}, retry=True, now=202)
    _, client = _client(store)
    page = client.get("/discovery").text
    for text in ("House", "2020", "128.0 BPM", "Energy 0.8", "Danceability 0.7"):
        assert text in page
    assert "Queued for enrichment" not in page and "Provider details" not in page


def test_track_charts_count_saved_fields_and_ignore_completion_stamps():
    from yt_playlist.web.routes.discovery import track_pool_summary

    summary = track_pool_summary([
        {"genre": "House", "year": "1999", "audio": {"energy": 0}, "last_enriched": None},
        {"genre": "house", "year": "1991", "audio": {}, "last_enriched": 100},
        {"genre": None, "year": "invalid", "audio": {}, "last_enriched": 100},
        {"genre": "Jazz", "year": "2026", "audio": {}, "last_enriched": None},
    ])
    assert [(r["label"], r["count"]) for r in summary["genres"]] == [("House", 2), ("Jazz", 1), ("No genre", 1)]
    assert [(r["label"], r["count"]) for r in summary["eras"]] == [("1990s", 2), ("2020s", 1), ("No year", 1)]
    assert [(r["label"], r["count"]) for r in summary["gaps"]] == [("No genre", 1), ("No year", 1), ("No audio features", 3)]
    assert summary["without_metadata"] == 1
    assert summary["genres"][0]["pct"] == 50
